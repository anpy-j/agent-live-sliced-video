from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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

OPENCODE_FALLBACK_MODELS = [
    ("opencode-go/gpt-5.6-luna", "gpt-5.6-luna"),
    ("opencode-go/glm-5.3", "glm-5.3"),
    ("openai/gpt-5.6-sol", "gpt-5.6-sol"),
    ("google/gemini-3.8-flash", "gemini-3.8-flash"),
    ("jysd/glm-5.3-flash", "glm-5.3-flash"),
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
                    "product": {"type": "string", "minLength": 1},
                    "color": {"type": "string"},
                },
                "required": ["src", "start", "end", "text", "role", "module", "product", "color"],
            },
        },
    },
    "required": ["main_product", "picks"],
}

VISUAL_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "block_id": {"type": "string", "minLength": 1},
                    "candidate_id": {"type": "string", "minLength": 1},
                    "reason": {"type": "string"},
                },
                "required": ["block_id", "candidate_id", "reason"],
            },
        },
    },
    "required": ["replacements"],
}


class ProviderResponseError(RuntimeError):
    """Provider completed, but its response could not become a LiveCut plan."""

    def __init__(self, message: str, raw: dict[str, Any]):
        super().__init__(message)
        self.raw = raw


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
            "vision_models": [{"id": model_id, "name": name}
                              for model_id, name in self.vision_models()],
        }

    def vision_models(self) -> list[tuple[str, str]]:
        return []

    def validate_vision_model(self, model: str) -> None:
        if model not in {item[0] for item in self.vision_models()}:
            raise ValueError(f"{self.display_name} 的模型 {model} 不支持当前多模态混剪调用")

    def generate_visual_plan(self, *, model: str, prompt: str, images: list[Path], cwd: Path,
                             on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                             timeout: int = 360) -> dict[str, Any]:
        raise ValueError(f"{self.display_name} 暂不支持多模态混剪")

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
            start_new_session=sys.platform != "win32",
        )
        if on_process:
            on_process(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise ProviderResponseError(
                f"{self.display_name} 编排超过 {timeout // 60} 分钟，已停止",
                {"stdout": (stdout or "")[-100000:], "stderr": (stderr or "")[-20000:],
                 "timeout_seconds": timeout, "returncode": process.returncode},
            ) from None
        if process.returncode:
            tail = (stderr or stdout).strip()[-1600:]
            raise ProviderResponseError(
                f"{self.display_name} 编排失败：{tail or f'退出码 {process.returncode}'}",
                {"stdout": (stdout or "")[-100000:], "stderr": (stderr or "")[-20000:],
                 "returncode": process.returncode},
            )
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
            clean = text
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE).strip()
            try:
                found = cls._find_plan(json.loads(clean))
                if found:
                    return found
            except (json.JSONDecodeError, TypeError):
                pass
            for match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE):
                try:
                    found = cls._find_plan(json.loads(match.group(1)))
                    if found:
                        return found
                except (json.JSONDecodeError, TypeError):
                    continue
            match = re.search(r"(\{[\s\S]*\"main_product\"[\s\S]*\"picks\"[\s\S]*\})", text)
            if match:
                try:
                    found = cls._find_plan(json.loads(match.group(1)))
                    if found:
                        return found
                except (json.JSONDecodeError, TypeError):
                    pass
            return None
        return None

    @classmethod
    def _find_visual_plan(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if isinstance(value.get("replacements"), list):
                return {"replacements": value["replacements"]}
            for key in ("structured_output", "result", "output", "output_text", "content",
                        "data", "message"):
                if key in value:
                    found = cls._find_visual_plan(value[key])
                    if found:
                        return found
            for nested in value.values():
                found = cls._find_visual_plan(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = cls._find_visual_plan(nested)
                if found:
                    return found
        elif isinstance(value, str):
            text = value.strip()
            clean = text
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean,
                              flags=re.IGNORECASE).strip()
            try:
                found = cls._find_visual_plan(json.loads(clean))
                if found:
                    return found
            except (json.JSONDecodeError, TypeError):
                pass
            for match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE):
                try:
                    found = cls._find_visual_plan(json.loads(match.group(1)))
                    if found:
                        return found
                except (json.JSONDecodeError, TypeError):
                    continue
            match = re.search(r"(\{[\s\S]*\"replacements\"[\s\S]*\})", text)
            if match:
                try:
                    found = cls._find_visual_plan(json.loads(match.group(1)))
                    if found:
                        return found
                except (json.JSONDecodeError, TypeError):
                    pass
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

    def vision_models(self) -> list[tuple[str, str]]:
        return [(model_id, name) for model_id, name in self.models()
                if model_id == "glm-5v-turbo"]

    def generate_visual_plan(self, *, model: str, prompt: str, images: list[Path], cwd: Path,
                             on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                             timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_vision_model(model)
        image_paths = "\n".join(f"- {path.resolve()}" for path in images)
        command = [
            str(self.executable), "-p", "--output-format", "json",
            "--json-schema", json.dumps(VISUAL_PLAN_SCHEMA, ensure_ascii=False,
                                         separators=(",", ":")),
            "--model", model, "--max-turns", "4", "--tools", "Read,StructuredOutput",
            "--add-dir", str(Path(cwd).resolve()),
            "--permission-mode", "dontAsk", "--no-session-persistence",
            f"{prompt}\n\n请使用 Read 查看以下图片：\n{image_paths}",
        ]
        started = time.monotonic()
        stdout, stderr, seconds = self._complete(
            command, cwd=cwd, on_process=on_process, timeout=timeout, started=started)
        envelope = self._parse_json(stdout)
        plan = self._find_visual_plan(envelope)
        if not plan:
            raise ProviderResponseError(
                "WorkBuddy 已返回结果，但没有找到 replacements",
                {"stdout": stdout[-100000:], "stderr": stderr[-20000:],
                 "envelope": envelope, "seconds": seconds, "model": model},
            )
        return {"plan": plan, "raw": envelope, "stderr": stderr.strip(), "seconds": seconds,
                "usage": self._find_usage(envelope)}

    def generate_plan(self, *, model: str, prompt: str, cwd: Path,
                      on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                      timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_model(model)
        command = [
            str(self.executable), "-p", "--output-format", "json",
            "--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--model", model, "--max-turns", "1", "--tools", "StructuredOutput",
            "--permission-mode", "dontAsk", "--no-session-persistence", prompt,
        ]
        started = time.monotonic()
        stdout, stderr, seconds = self._complete(
            command, cwd=cwd, on_process=on_process, timeout=timeout, started=started)
        envelope = self._parse_json(stdout)
        plan = self._find_plan(envelope)
        if not plan:
            raise ProviderResponseError(
                "WorkBuddy 已返回结果，但没有找到 main_product 和 picks",
                {"stdout": stdout[-100000:], "stderr": stderr[-20000:],
                 "envelope": envelope, "seconds": seconds, "model": model},
            )
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

    def vision_models(self) -> list[tuple[str, str]]:
        return self.models()

    def generate_visual_plan(self, *, model: str, prompt: str, images: list[Path], cwd: Path,
                             on_process: Callable[[subprocess.Popen[str]], None] | None = None,
                             timeout: int = 360) -> dict[str, Any]:
        self._ensure_available()
        self.validate_vision_model(model)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="livecut-codex-vision-") as temp_dir:
            temp = Path(temp_dir)
            schema_path = temp / "visual-schema.json"
            output_path = temp / "visual-plan.json"
            schema_path.write_text(json.dumps(VISUAL_PLAN_SCHEMA, ensure_ascii=False),
                                   encoding="utf-8")
            command = [
                str(self.executable), "exec", "--json", "--color", "never",
                "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-C", str(temp),
            ]
            for image_path in images:
                command.extend(["--image", str(image_path.resolve())])
            if model != "auto":
                command.extend(["--model", model])
            command.append(prompt)
            stdout, stderr, seconds = self._complete(
                command, cwd=temp, on_process=on_process, timeout=timeout, started=started)
            if not output_path.is_file():
                raise RuntimeError("Codex 已结束，但没有生成多模态混剪结果")
            plan = self._parse_json(output_path.read_text(encoding="utf-8"))
            events = []
            for line in stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        normalized = self._find_visual_plan(plan)
        if not normalized:
            raise RuntimeError("Codex 已返回结果，但没有找到 replacements")
        return {"plan": normalized, "raw": {"result": plan, "events": events},
                "stderr": stderr.strip(), "seconds": seconds, "usage": self._find_usage(events)}

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
        choices = [("auto", "默认配置"), *OPENCODE_FALLBACK_MODELS]
        if self.executable.is_file() and os.access(self.executable, os.X_OK):
            try:
                result = subprocess.run(
                    [str(self.executable), "models"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=20,
                    env={**os.environ, "TERM": "xterm", "NO_COLOR": "1"},
                )
                discovered = []
                seen = set()
                for line in result.stdout.splitlines():
                    model_id = line.strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+/\S+", model_id) or model_id in seen:
                        continue
                    seen.add(model_id)
                    discovered.append((model_id, model_id.split("/", 1)[1]))
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
        constrained_prompt = f"""你现在是一个只返回 JSON 的编排接口，不是聊天助手。

最高优先级输出契约：
1. 第一个字符必须是 {{，最后一个字符必须是 }}。
2. 顶层必须同时包含非空字符串 main_product 和非空数组 picks，字段名不得翻译、改名或省略。
3. picks 的每一项必须包含 src、start、end、text、role、module、product、color 八个字段。
4. 不得输出分析、解释、道歉、Markdown、代码围栏或 JSON 之外的任何字符。
5. 即使候选不完美，也必须选择最接近约束的最佳完整方案并返回上述对象；不得只描述方案或拒绝作答。

{prompt}

最终响应只允许是一个符合以下 Schema 的 JSON 对象：
{schema}

再次确认：必须返回 main_product 和 picks；只输出 JSON 对象。"""
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
            raise ProviderResponseError(
                "OpenCode 已返回结果，但没有找到 main_product 和 picks",
                {"stdout": stdout[-100000:], "stderr": stderr[-20000:],
                 "events": events[-100:], "seconds": seconds, "model": model},
            )
        return {"plan": plan, "raw": {"events": events}, "stderr": stderr.strip(),
                "seconds": seconds, "usage": self._find_usage(events)}
