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
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agent_video.ai import (AntigravityCli, CodexCli, OpenCodeCli,
                            ProviderResponseError, WorkBuddyCli)

from .errors import AIReturnError, PipelineConfigError

DEFAULT_TIMEOUT = 90
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

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "main_product": {"type": "string", "minLength": 1},
        "ordered_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "integer", "minimum": 0},
        },
    },
    "required": ["main_product", "ordered_ids"],
}

_CLI_BUILDERS = {
    "workbuddy": WorkBuddyCli,
    "antigravity": AntigravityCli,
    "codex": CodexCli,
    "opencode": OpenCodeCli,
}

_FALLBACK_EXECUTABLES = {
    "workbuddy": "/Applications/AI/WorkBuddy.app/Contents/Resources/bin/codebuddy",
    "antigravity": str(Path.home() / ".local" / "bin" / "agy"),
    "codex": "/opt/homebrew/bin/codex",
    "opencode": str(Path.home() / ".opencode" / "bin" / "opencode"),
}

_WHICH = {
    "workbuddy": "codebuddy",
    "antigravity": "agy",
    "codex": "codex",
    "opencode": "opencode",
}


def _provider_executable(provider_id: str) -> str:
    found = shutil.which(_WHICH.get(provider_id, provider_id))
    if found:
        return found
    return _FALLBACK_EXECUTABLES.get(provider_id, "")


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
    if chosen_model != "auto":
        try:
            provider.validate_model(chosen_model)
        except ValueError as exc:
            raise PipelineConfigError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="lean-pipeline-ai-") as temp_dir:
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
        else:
            raise PipelineConfigError(f"Jev 引擎暂不支持 schema 字段：{name}")
    return questions


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
