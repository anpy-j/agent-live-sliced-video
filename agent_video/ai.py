from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


WORKBUDDY_MODELS = [
    ("auto", "自动选择"),
    ("glm-5v-turbo", "GLM 5V Turbo"),
    ("glm-5.1", "GLM 5.1"),
    ("glm-5.0-turbo", "GLM 5.0 Turbo"),
    ("glm-5.0", "GLM 5.0"),
    ("glm-4.7", "GLM 4.7"),
    ("kimi-k2.5", "Kimi K2.5"),
    ("minimax-m2.7", "MiniMax M2.7"),
    ("deepseek-v3-2-volc", "DeepSeek V3.2"),
]


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "main_product": {"type": "string", "minLength": 1},
        "picks": {
            "type": "array",
            "minItems": 2,
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "src": {"type": "integer", "minimum": 1},
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "text": {"type": "string", "minLength": 1},
                    "role": {"type": "string", "enum": [
                        "hook", "result", "pain", "proof", "fit", "material", "craft",
                        "color", "styling", "scene", "demo", "close", "bridge",
                    ]},
                    "module": {"type": "string", "enum": ["hook_A", "body"]},
                },
                "required": ["src", "start", "end", "text", "role", "module"],
            },
        },
    },
    "required": ["main_product", "picks"],
}


class WorkBuddyCli:
    def __init__(self, executable: Path):
        self.executable = Path(executable).expanduser()

    def info(self) -> dict[str, Any]:
        return {
            "id": "workbuddy",
            "name": "WorkBuddy CLI",
            "available": self.executable.is_file() and os.access(self.executable, os.X_OK),
            "path": str(self.executable),
            "models": [{"id": model_id, "name": name} for model_id, name in WORKBUDDY_MODELS],
        }

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        if not self.info()["available"]:
            raise RuntimeError(f"WorkBuddy CLI 不可用: {self.executable}")
        allowed = {item[0] for item in WORKBUDDY_MODELS}
        if model not in allowed:
            raise ValueError(f"WorkBuddy 不支持模型: {model}")
        command = [
            str(self.executable), "-p", "--output-format", "json",
            "--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--model", model, "--max-turns", "1", "--tools", "",
            "--permission-mode", "dontAsk", "--no-session-persistence", prompt,
        ]
        started = time.monotonic()
        process = subprocess.Popen(
            command, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "NO_COLOR": "1"},
        )
        if on_process:
            on_process(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError("WorkBuddy AI 编排超过 6 分钟，已停止") from None
        if process.returncode:
            tail = (stderr or stdout).strip()[-1200:]
            raise RuntimeError(f"WorkBuddy AI 编排失败：{tail or f'退出码 {process.returncode}'}")
        envelope = self._parse_json(stdout)
        plan = self._find_plan(envelope)
        if not plan:
            raise RuntimeError("WorkBuddy 已返回结果，但没有找到 main_product 和 picks")
        usage = self._find_usage(envelope)
        return {
            "plan": plan,
            "raw": envelope,
            "stderr": stderr.strip(),
            "seconds": round(time.monotonic() - started, 2),
            "usage": usage,
        }

    @classmethod
    def _parse_json(cls, value: str) -> Any:
        text = value.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise RuntimeError("WorkBuddy 没有返回有效 JSON") from None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                raise RuntimeError("WorkBuddy 返回的 JSON 无法解析") from None

    @classmethod
    def _find_plan(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if isinstance(value.get("main_product"), str) and isinstance(value.get("picks"), list):
                return {"main_product": value["main_product"], "picks": value["picks"]}
            for key in ("structured_output", "result", "output", "content", "data", "message"):
                if key in value:
                    found = cls._find_plan(value[key])
                    if found:
                        return found
            for nested in value.values():
                found = cls._find_plan(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = cls._find_plan(nested)
                if found:
                    return found
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            try:
                return cls._find_plan(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    @classmethod
    def _find_usage(cls, value: Any) -> dict[str, int]:
        result = {"input_tokens": 0, "output_tokens": 0}
        if isinstance(value, dict):
            for key, target in (("input_tokens", "input_tokens"), ("prompt_tokens", "input_tokens"),
                                ("output_tokens", "output_tokens"), ("completion_tokens", "output_tokens")):
                if isinstance(value.get(key), (int, float)):
                    result[target] = max(result[target], int(value[key]))
            for nested in value.values():
                usage = cls._find_usage(nested)
                result["input_tokens"] = max(result["input_tokens"], usage["input_tokens"])
                result["output_tokens"] = max(result["output_tokens"], usage["output_tokens"])
        elif isinstance(value, list):
            for nested in value:
                usage = cls._find_usage(nested)
                result["input_tokens"] = max(result["input_tokens"], usage["input_tokens"])
                result["output_tokens"] = max(result["output_tokens"], usage["output_tokens"])
        return result
