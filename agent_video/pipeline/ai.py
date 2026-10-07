# -*- coding: utf-8 -*-
"""AI 调用边界：单一无状态原语 ``ai_call(model, prompt, json_schema, timeout)``。

设计约束（对应 issue 的「AI 调用边界」）：
  - 不逐批、不用 agent 多轮、不注入 memory/历史；
  - 固定模型、固定 schema，任何非法返回 = 明确报错并停止；
  - 不自动修复、不静默兜底。

两种实现共享同一签名与同一 schema：
  - ``llm``：走仓库既有 CLI provider（codex/opencode/workbuddy/antigravity），
    经 ``CliProvider.generate_json`` 做一次原生 schema JSON 调用；
  - ``jev``：走 TypeSafe Jev 的 ``system_one``，把 schema 翻译成问答。

选择通过 ``PIPELINE_AI_ENGINE``（llm/jev，默认 llm）与 ``PIPELINE_AI_PROVIDER``
控制。旧编排的多 provider 自动降级链不在这里复现：任一 provider 失败即报错。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_video.ai import (AntigravityCli, CodexCli, OpenCodeCli,
                            DEFAULT_AI_TIMEOUT_SECONDS, ProviderResponseError,
                            WorkBuddyCli, _workbuddy_cli_entries)

from .errors import AIReturnError, PipelineConfigError

def _default_timeout() -> int:
    """单次 AI 调用上限，秒。

    与旧 provider 层同源（``DEFAULT_AI_TIMEOUT_SECONDS``）：WorkBuddy 在素材
    较大、并发较高时单批可能远超 6 分钟，过短的上限会把正常但较慢的调用误判
    为失败。可用 ``PIPELINE_AI_TIMEOUT`` 覆盖。
    """
    raw = os.environ.get("PIPELINE_AI_TIMEOUT")
    if raw:
        try:
            value = int(float(raw))
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_AI_TIMEOUT_SECONDS


DEFAULT_TIMEOUT = _default_timeout()
PROVIDER_ORDER = ("opencode", "codex", "workbuddy", "antigravity")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "minimum": 0},
                    "usable": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "usable", "reason"],
            },
        },
    },
    "required": ["decisions"],
}

# S4 成片结构角色：hook 必须开头，cta 若存在必须收尾，其余为可选中段。
ORDER_SECTION_ROLES: tuple[str, ...] = (
    "hook", "scene", "selling_point", "proof", "styling", "cta")

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "main_product": {"type": "string", "minLength": 1},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "role": {"type": "string", "enum": list(ORDER_SECTION_ROLES)},
                    "ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 0},
                    },
                },
                "required": ["role", "ids"],
            },
        },
        "ordered_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "integer", "minimum": 0},
        },
    },
    "required": ["main_product", "sections", "ordered_ids"],
}

_CLI_BUILDERS = {
    "workbuddy": WorkBuddyCli,
    "antigravity": AntigravityCli,
    "codex": CodexCli,
    "opencode": OpenCodeCli,
}

_FALLBACK_EXECUTABLES: dict[str, tuple[str, ...]] = {
    # WorkBuddy 5.x 把 CLI 放进了 app.asar.unpacked/cli/bin；保留旧路径兼容旧版本。
    "workbuddy": (
        *((str(Path.home() / "AppData" / "Local" / "Programs" / "WorkBuddy" / "resources"
               / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"),)
          if sys.platform == "win32" else ()),
        "/Applications/AI/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
        "/Applications/AI/WorkBuddy.app/Contents/Resources/bin/codebuddy",
        str(Path.home() / "Applications" / "WorkBuddy.app" / "Contents"
            / "Resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"),
    ),
    "antigravity": (
        str(Path.home() / ".local" / "bin" / "agy"),
        str(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"),
    ),
    "codex": (
        "/opt/homebrew/bin/codex",
        str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
    ),
    "opencode": (
        str(Path.home() / ".opencode" / "bin" / "opencode"),
        str(Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd"),
    ),
}

_WHICH = {
    "workbuddy": "codebuddy",
    "antigravity": "agy",
    "codex": "codex",
    "opencode": "opencode",
}


def _provider_executable(provider_id: str) -> str:
    if provider_id == "workbuddy":
        for entry in _workbuddy_cli_entries():
            if entry.is_file():
                return str(entry)
    found = shutil.which(_WHICH.get(provider_id, provider_id))
    if found:
        return found
    candidates = [os.path.expanduser(path)
                  for path in _FALLBACK_EXECUTABLES.get(provider_id, ())]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # 都不存在时返回首选路径，让报错信息指出期望位置。
    return candidates[0] if candidates else ""


def resolve_provider(provider: str | None = None) -> tuple[str, str]:
    """返回 ``(provider_id, executable)``；auto 时按可用性挑第一个。"""
    requested = (provider or os.environ.get("PIPELINE_AI_PROVIDER") or "auto").strip().lower()
    if requested and requested != "auto":
        if requested not in _CLI_BUILDERS:
            raise PipelineConfigError(f"不支持的 AI provider: {requested}")
        executable = _provider_executable(requested)
        if not executable or not os.path.isfile(executable):
            raise PipelineConfigError(f"AI provider {requested} 不可用：{executable or '未找到可执行文件'}")
        return requested, executable
    for candidate in PROVIDER_ORDER:
        executable = _provider_executable(candidate)
        if executable and os.path.isfile(executable):
            return candidate, executable
    raise PipelineConfigError("没有可用的 AI provider（codex/opencode/workbuddy/antigravity）")


def _call_llm(model: str, prompt: str, json_schema: dict[str, Any],
              timeout: int) -> dict[str, Any]:
    provider_id, executable = resolve_provider()
    provider = _CLI_BUILDERS[provider_id](Path(executable))
    chosen_model = (model or os.environ.get("PIPELINE_AI_MODEL") or "auto").strip() or "auto"
    if hasattr(provider, "normalize_model"):
        chosen_model = provider.normalize_model(chosen_model)
    if chosen_model != "auto":
        try:
            provider.validate_model(chosen_model)
        except ValueError as exc:
            raise PipelineConfigError(str(exc)) from exc
    # ignore_cleanup_errors：调用目录可能位于易失的临时根（如任务沙箱）下，被外部
    # 清理后 rmtree 会 FileNotFoundError；这不该把一次已完成的调用判成失败。
    with tempfile.TemporaryDirectory(prefix="lean-pipeline-ai-",
                                     ignore_cleanup_errors=True) as temp_dir:
        try:
            result = provider.generate_json(
                model=chosen_model, prompt=prompt, schema=json_schema,
                cwd=Path(temp_dir), timeout=timeout)
        except ProviderResponseError as exc:
            raise AIReturnError(f"{provider_id} AI 调用失败：{exc}") from exc
        except (OSError, RuntimeError) as exc:
            raise AIReturnError(f"{provider_id} AI 调用失败：{exc}") from exc
    return result["data"]


def _schema_to_questions(schema: dict[str, Any]) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for name, spec in (schema.get("properties") or {}).items():
        kind = spec.get("type")
        if kind == "boolean":
            questions[name] = {"type": "choice", "instructions": f"判断 {name}",
                               "criteria": {"yes": "是", "no": "否"}}
        elif kind == "string":
            questions[name] = {"type": "text", "instructions": f"给出 {name}"}
        elif kind == "integer":
            questions[name] = {"type": "noul", "instructions": f"给出 {name}"}
        elif kind == "array" and (spec.get("items") or {}).get("type") == "integer":
            questions[name] = {"type": "text",
                               "instructions": f"给出 {name}：只输出整数 id，用英文逗号分隔"}
        elif kind == "array" and (spec.get("items") or {}).get("type") == "object":
            role_spec = (((spec.get("items") or {}).get("properties") or {}).get("role") or {})
            roles = role_spec.get("enum") or []
            role_hint = (f"role 只能取 {'/'.join(str(role) for role in roles)}"
                         if roles else "role 使用简短、贴合实际内容的中文分类名")
            questions[name] = {
                "type": "text",
                "instructions": (
                    f"给出 {name}：每段写成 role:id1,id2 的形式，多个段之间用英文分号 ; "
                    f"分隔；{role_hint}")}
        else:
            raise PipelineConfigError(f"Jev 引擎暂不支持 schema 字段：{name}")
    return questions


def _parse_sections_text(text: str) -> list[dict[str, Any]]:
    """把「role:id,id;role:id」文本解析成 S4 的 sections 结构。"""
    sections: list[dict[str, Any]] = []
    for chunk in re.split(r"[;\n；]+", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        role, separator, rest = chunk.partition(":")
        if not separator:
            role, separator, rest = chunk.partition("：")
        if not separator:
            continue
        ids = [int(part) for part in re.findall(r"\d+", rest)]
        if role.strip() and ids:
            sections.append({"role": role.strip(), "ids": ids})
    return sections


def _answers_to_object(schema: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    answers = response.get("answers") or {}
    result: dict[str, Any] = {}
    for name, spec in (schema.get("properties") or {}).items():
        answer = answers.get(name) or {}
        kind = spec.get("type")
        if kind == "boolean":
            result[name] = str(answer.get("choice")) in {"yes", "true", "是"}
        elif kind == "array":
            text = str(answer.get("text") or answer.get("choice") or "")
            if (spec.get("items") or {}).get("type") == "object":
                result[name] = _parse_sections_text(text)
            else:
                result[name] = [int(part) for part in (piece.strip() for piece in text.split(","))
                                if part.lstrip("-").isdigit()]
        elif kind == "integer":
            raw = answer.get("score", answer.get("value", answer.get("noul")))
            result[name] = int(raw) if raw is not None else 0
        else:
            result[name] = answer.get("choice") or answer.get("text") or ""
    return result


def _call_jev(model: str, prompt: str, json_schema: dict[str, Any],
              timeout: int) -> dict[str, Any]:
    from agent_video.jev import JevClient, JevError

    api_key = os.environ.get("TYPESAFE_API_KEY", "")
    if not api_key:
        raise PipelineConfigError("Jev 引擎需要 TYPESAFE_API_KEY")
    questions = _schema_to_questions(json_schema)
    try:
        client = JevClient(api_key,
                           base_url=os.environ.get("TYPESAFE_BASE_URL") or "",
                           timeout=float(timeout),
                           default_model=model or "jev-latest")
        response = client.system_one(state=prompt, questions=questions,
                                     model=model or None)
    except JevError as exc:
        raise AIReturnError(f"Jev AI 调用失败：{exc}") from exc
    return _answers_to_object(json_schema, response)


def ai_call(model: str, prompt: str, json_schema: dict[str, Any],
            timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """一次无状态 AI 调用，返回符合 ``json_schema`` 顶层契约的对象。"""
    engine = (os.environ.get("PIPELINE_AI_ENGINE") or "llm").strip().lower()
    if engine == "llm":
        data = _call_llm(model, prompt, json_schema, timeout)
    elif engine == "jev":
        data = _call_jev(model, prompt, json_schema, timeout)
    else:
        raise PipelineConfigError(f"不支持的 AI 引擎: {engine}")
    if not isinstance(data, dict):
        raise AIReturnError(f"AI 返回不是 JSON 对象：{type(data).__name__}")
    return data
