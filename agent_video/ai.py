from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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

ANTIGRAVITY_FALLBACK_MODELS = [
    ("auto", "默认配置"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
    ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
]

CODEX_MODELS = [
    ("auto", "默认配置"),
    ("gpt-6-astra", "GPT-6 Astra"),
    ("gpt-5.6-sol", "GPT-5.6 Sol"),
    ("gpt-5.6-terra", "GPT-5.6 Terra"),
    ("gpt-5.6-luna", "GPT-5.6 Luna"),
    ("gpt-5.5", "GPT-5.5"),
]

OPENCODE_RECOMMENDED_MODELS = [
    ("opencode-go/gpt-5.6-luna", "OpenCode Go · GPT-5.6 Luna"),
    ("opencode-go/glm-5.3", "OpenCode Go · GLM 5.3"),
    ("opencode-go/kimi-k3", "OpenCode Go · Kimi K3"),
    ("opencode-go/deepseek-v4-pro", "OpenCode Go · DeepSeek V4 Pro"),
    ("opencode-go/minimax-m3", "OpenCode Go · MiniMax M3"),
    ("openai/gpt-6-astra", "OpenAI · GPT-6 Astra"),
    ("openai/gpt-5.6-sol", "OpenAI · GPT-5.6 Sol"),
    ("openai/gpt-5.6-terra", "OpenAI · GPT-5.6 Terra"),
    ("openai/gpt-5.6-luna", "OpenAI · GPT-5.6 Luna"),
    ("google/gemini-3.8-flash", "Google · Gemini 3.8 Flash"),
    ("google/gemini-3.1-pro-preview", "Google · Gemini 3.1 Pro Preview"),
    ("kimi-for-coding/k3", "Kimi · K3"),
    ("deepseek/deepseek-v4-pro", "DeepSeek · V4 Pro"),
    ("opencode/big-pickle", "OpenCode · Big Pickle"),
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


class CliProvider:
    provider_id = ""
    display_name = ""
    model_choices: list[tuple[str, str]] = []

    def __init__(self, executable: Path):
        self.executable = Path(executable).expanduser()

    def models(self) -> list[tuple[str, str]]:
        return self.model_choices

    def info(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self.display_name,
            "available": self.executable.is_file() and os.access(self.executable, os.X_OK),
            "path": str(self.executable),
            "models": [{"id": model_id, "name": name} for model_id, name in self.models()],
        }

    def validate_model(self, model: str) -> None:
        if model not in {item[0] for item in self.models()}:
            raise ValueError(f"{self.display_name} 不支持模型: {model}")

    def _ensure_available(self) -> None:
        if not self.info()["available"]:
            raise RuntimeError(f"{self.display_name} 不可用: {self.executable}")

    def _complete(self, command: list[str], *, cwd: Path,
                  on_process: Callable[[subprocess.Popen[str]], None] | None,
                  timeout: int, started: float,
                  env_overrides: dict[str, str] | None = None) -> tuple[str, str, float]:
        process = subprocess.Popen(
            command, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "NO_COLOR": "1", "TERM": "xterm", **(env_overrides or {})},
        )
        if on_process:
            on_process(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError(f"{self.display_name} 编排超过 {timeout // 60} 分钟，已停止") from None
        if process.returncode:
            tail = (stderr or stdout).strip()[-1600:]
            raise RuntimeError(f"{self.display_name} 编排失败：{tail or f'退出码 {process.returncode}'}")
        return stdout, stderr, round(time.monotonic() - started, 2)

    @classmethod
    def _parse_json(cls, value: str) -> Any:
        text = value.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise RuntimeError("AI 没有返回有效 JSON") from None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                raise RuntimeError("AI 返回的 JSON 无法解析") from None

    @classmethod
    def _find_plan(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if isinstance(value.get("main_product"), str) and isinstance(value.get("picks"), list):
                return {"main_product": value["main_product"], "picks": value["picks"]}
            for key in ("structured_output", "result", "output", "output_text", "content", "data", "message"):
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
            tokens = value.get("tokens")
            if isinstance(tokens, dict):
                if isinstance(tokens.get("input"), (int, float)):
                    result["input_tokens"] = int(tokens["input"])
                if isinstance(tokens.get("output"), (int, float)):
                    result["output_tokens"] = int(tokens["output"])
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


class WorkBuddyCli(CliProvider):
    provider_id = "workbuddy"
    display_name = "WorkBuddy CLI"
    model_choices = WORKBUDDY_MODELS

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_model(model)
        command = [
            str(self.executable), "-p", "--output-format", "json",
            "--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--model", model, "--max-turns", "1", "--tools", "",
            "--permission-mode", "dontAsk", "--no-session-persistence", prompt,
        ]
        started = time.monotonic()
        stdout, stderr, seconds = self._complete(
            command, cwd=cwd, on_process=on_process, timeout=timeout, started=started)
        envelope = self._parse_json(stdout)
        plan = self._find_plan(envelope)
        if not plan:
            raise RuntimeError("WorkBuddy 已返回结果，但没有找到 main_product 和 picks")
        return {"plan": plan, "raw": envelope, "stderr": stderr.strip(), "seconds": seconds,
                "usage": self._find_usage(envelope)}


class AntigravityCli(CliProvider):
    provider_id = "antigravity"
    display_name = "Antigravity CLI"
    _model_cache: tuple[float, list[tuple[str, str]]] | None = None

    def models(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        cache = type(self)._model_cache
        if cache and now - cache[0] < 300:
            return cache[1]
        choices = ANTIGRAVITY_FALLBACK_MODELS
        if self.executable.is_file() and os.access(self.executable, os.X_OK):
            try:
                result = subprocess.run(
                    [str(self.executable), "models"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15,
                    env={**os.environ, "TERM": "xterm", "NO_COLOR": "1"},
                )
                parsed = []
                for line in result.stdout.splitlines():
                    parts = line.strip().split("\t", 1)
                    if len(parts) == 2 and parts[0] and not parts[0].startswith("Fetching"):
                        parsed.append((parts[0], parts[1]))
                if parsed:
                    choices = [("auto", "默认配置"), *parsed]
            except (OSError, subprocess.TimeoutExpired):
                pass
        type(self)._model_cache = (now, choices)
        return choices

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_model(model)
        command = [
            str(self.executable), "-p", prompt, "--output-format", "json",
            "--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--print-timeout", f"{timeout}s", "--sandbox", "--disable-slash-commands",
        ]
        if model != "auto":
            command.extend(["--model", model])
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="livecut-antigravity-") as temp_dir:
            stdout, stderr, seconds = self._complete(
                command, cwd=Path(temp_dir), on_process=on_process, timeout=timeout, started=started)
        envelope = self._parse_json(stdout)
        plan = self._find_plan(envelope)
        if not plan:
            raise RuntimeError("Antigravity 已返回结果，但没有找到 main_product 和 picks")
        return {"plan": plan, "raw": envelope, "stderr": stderr.strip(), "seconds": seconds,
                "usage": self._find_usage(envelope)}


class CodexCli(CliProvider):
    provider_id = "codex"
    display_name = "Codex CLI"
    model_choices = CODEX_MODELS

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_model(model)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="livecut-codex-") as temp_dir:
            temp = Path(temp_dir)
            schema_path = temp / "plan-schema.json"
            output_path = temp / "plan.json"
            schema_path.write_text(json.dumps(PLAN_SCHEMA, ensure_ascii=False), encoding="utf-8")
            command = [
                str(self.executable), "exec", "--json", "--color", "never",
                "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-C", str(temp),
            ]
            if model != "auto":
                command.extend(["--model", model])
            command.append(prompt)
            stdout, stderr, seconds = self._complete(
                command, cwd=temp, on_process=on_process, timeout=timeout, started=started)
            if not output_path.is_file():
                raise RuntimeError("Codex 已结束，但没有生成结构化编排结果")
            plan = self._parse_json(output_path.read_text(encoding="utf-8"))
            events = []
            for line in stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        normalized = self._find_plan(plan)
        if not normalized:
            raise RuntimeError("Codex 已返回结果，但没有找到 main_product 和 picks")
        return {"plan": normalized, "raw": {"result": plan, "events": events},
                "stderr": stderr.strip(), "seconds": seconds, "usage": self._find_usage(events)}


class OpenCodeCli(CliProvider):
    provider_id = "opencode"
    display_name = "OpenCode CLI"
    _model_cache: tuple[float, list[tuple[str, str]]] | None = None

    def models(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        cache = type(self)._model_cache
        if cache and now - cache[0] < 300:
            return cache[1]
        choices = [("auto", "默认配置"), *OPENCODE_RECOMMENDED_MODELS]
        if self.executable.is_file() and os.access(self.executable, os.X_OK):
            try:
                result = subprocess.run(
                    [str(self.executable), "models"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=20,
                    env={**os.environ, "TERM": "xterm", "NO_COLOR": "1"},
                )
                available = {line.strip() for line in result.stdout.splitlines() if "/" in line}
                discovered = [item for item in OPENCODE_RECOMMENDED_MODELS if item[0] in available]
                if discovered:
                    choices = [("auto", "默认配置"), *discovered]
            except (OSError, subprocess.TimeoutExpired):
                pass
        type(self)._model_cache = (now, choices)
        return choices

    @staticmethod
    def _runtime_config() -> str:
        return json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "plugin": [],
            "agent": {
                "livecut": {
                    "description": "Return one structured LiveCut edit plan without using tools.",
                    "mode": "primary",
                    "steps": 1,
                    "permission": {"*": "deny"},
                    "tools": {"*": False},
                },
            },
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _parse_events(cls, stdout: str) -> tuple[list[Any], dict[str, Any] | None]:
        events: list[Any] = []
        text_parts: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            plan = cls._find_plan(event)
            if plan:
                return events, plan
            if isinstance(event, dict):
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif isinstance(event.get("text"), str):
                    text_parts.append(event["text"])
        return events, cls._find_plan("".join(text_parts)) if text_parts else None

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_model(model)
        schema = json.dumps(PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        constrained_prompt = f"{prompt}\n\n只输出 JSON，不要 Markdown。输出必须符合此 JSON Schema：{schema}"
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="livecut-opencode-") as temp_dir:
            temp = Path(temp_dir)
            command = [
                str(self.executable), "run", "--format", "json", "--pure",
                "--agent", "livecut", "--dir", str(temp),
            ]
            if model != "auto":
                command.extend(["--model", model])
            command.append(constrained_prompt)
            stdout, stderr, seconds = self._complete(
                command, cwd=temp, on_process=on_process, timeout=timeout, started=started,
                env_overrides={"OPENCODE_CONFIG_CONTENT": self._runtime_config()},
            )
        events, plan = self._parse_events(stdout)
        if not plan:
            raise RuntimeError("OpenCode 已返回结果，但没有找到 main_product 和 picks")
        return {"plan": plan, "raw": {"events": events}, "stderr": stderr.strip(),
                "seconds": seconds, "usage": self._find_usage(events)}
