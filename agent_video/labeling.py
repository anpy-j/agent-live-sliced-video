# -*- coding: utf-8 -*-
"""S2 标注工作台：把人工判定回灌成可复用的词表规则。

S2 是确定性粗筛（``pipeline.filter``），规则全部来自 ``badvocab`` 词表与
``textnorm`` 内置正则。工作台只做三件事：

1. ``prepare``：对素材跑 S1（ASR+切分）与 S2，给出每条子句的判定、原因与命中词；
2. ``build_patch``：对比人工判定与 S2 判定，把**人工圈出的词**翻译成词表增删；
3. ``apply_patch``：把补丁合并进 ``profiles/label-overrides.json`` 并热加载词表。

只处理可由词表解释的差异；结构性命中（时长/重复/中文字符占比）无法靠词表修复，
一律进 ``unresolved``。工作台**不自动应用**，由调用方显式调用 ``apply_patch``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine.scripts import badvocab
from .pipeline import asr, split
from .pipeline.filter import filter_clauses, reject_hit

# 结构性命中：无法通过词表增删修复。
_STRUCTURAL_REASONS = {"too_short", "non_chinese", "duration_gate", "invalid_bounds",
                       "duplicate"}
# 内置正则命中：不是词表，不能靠增删词修复。
_REGEX_REASONS = {"stage_chatter", "malformed_speech"}

Patch = dict[str, list]


def overrides_path() -> Path:
    return Path(__file__).resolve().parent / "engine" / "profiles" / "label-overrides.json"


def activate_overrides() -> dict[str, Any]:
    """服务启动时调用：若存在标注补丁，设为当前词表配置并热加载。"""
    path = overrides_path()
    if path.is_file():
        badvocab.reload_profile(path)
    return {"path": str(path), "active": path.is_file()}


def profile_summary() -> dict[str, Any]:
    return {"summary": badvocab.summary(), "overrides": load_overrides()}


def prepare(source_path: str, workdir: str, *, backend: str | None = None,
            model: str | None = None) -> list[dict[str, Any]]:
    """跑 S1+S2，返回带 ``usable``/``reason``/``hit`` 的子句列表（不落库）。"""
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)
    sentences, words = asr.transcribe(source_path, str(workdir_path), backend=backend,
                                      model=model)
    clauses = split.split_clauses(words, sentences)
    filter_clauses(clauses)
    for clause in clauses:
        clause["hit"] = reject_hit(clause)
    return clauses


def load_overrides() -> dict[str, Any]:
    path = overrides_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unresolved(clause: dict[str, Any], decision: dict[str, Any],
                detail: str) -> dict[str, Any]:
    return {"id": clause.get("id"), "text": clause.get("text"),
            "s2_usable": bool(clause.get("usable")), "label": bool(decision.get("label")),
            "reason": clause.get("reason") or "", "detail": detail}


def _tokens(decision: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        str(token).strip() for token in (decision.get("tokens") or []) if str(token).strip()
    ))


def build_patch(clauses: list[dict[str, Any]],
                decisions: dict[str, Any]) -> Patch:
    """对比人工判定与 S2 判定，产出词表补丁。

    ``decisions`` 形如 ``{"12": {"label": false, "tokens": ["多少钱"], "regex": false}}``，
    只处理 ``label`` 与 S2 判定不一致、且已标注的子句。
    """
    patch: Patch = {"hard_add": [], "hard_remove": [], "hard_regex_add": [],
                    "hard_regex_remove": [], "unresolved": []}
    for clause in clauses:
        decision = decisions.get(str(clause.get("id")))
        if not decision or decision.get("label") is None:
            continue
        s2_usable = bool(clause.get("usable"))
        label = bool(decision["label"])
        if label == s2_usable:
            continue
        tokens = _tokens(decision)
        as_regex = bool(decision.get("regex"))
        reason = str(clause.get("reason") or "")

        if s2_usable and not label:
            # 规则误放行：用户圈出的词是新增硬禁词。
            if not tokens:
                patch["unresolved"].append(
                    _unresolved(clause, decision, "需圈出误放行的词才能新增规则"))
                continue
            (patch["hard_regex_add"] if as_regex else patch["hard_add"]).extend(tokens)
        else:
            # 规则误杀：移除导致命中的词。
            if reason in _REGEX_REASONS:
                patch["unresolved"].append(
                    _unresolved(clause, decision, "话术/病句为内置正则，不支持词表移除"))
                continue
            if reason in _STRUCTURAL_REASONS:
                patch["unresolved"].append(
                    _unresolved(clause, decision, "结构性命中无法靠词表修复"))
                continue
            remove = tokens or ([clause["hit"]] if clause.get("hit") else [])
            if not remove:
                patch["unresolved"].append(
                    _unresolved(clause, decision, "未找到可移除的命中词"))
                continue
            (patch["hard_regex_remove"] if as_regex else patch["hard_remove"]).extend(remove)

    for key in ("hard_add", "hard_remove", "hard_regex_add", "hard_regex_remove"):
        patch[key] = list(dict.fromkeys(patch[key]))
    return patch


def merge_patch(current: dict[str, Any], patch: Patch) -> dict[str, Any]:
    """合并补丁；移除优先（同一词既增又删时按移除处理）。"""
    add = set(current.get("hard_add") or []) | set(patch.get("hard_add") or [])
    add_regex = (set(current.get("hard_regex_add") or [])
                 | set(patch.get("hard_regex_add") or []))
    remove = set(current.get("hard_remove") or []) | set(patch.get("hard_remove") or [])
    remove_regex = (set(current.get("hard_regex_remove") or [])
                    | set(patch.get("hard_regex_remove") or []))
    add -= remove
    add_regex -= remove_regex
    return {
        "hard_add": sorted(add),
        "hard_remove": sorted(remove),
        "hard_regex_add": sorted(add_regex),
        "hard_regex_remove": sorted(remove_regex),
        "source": "label-workbench",
    }


def apply_patch(patch: Patch) -> dict[str, Any]:
    """把补丁写进覆盖配置并热加载词表。"""
    merged = merge_patch(load_overrides(), patch)
    path = overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    badvocab.reload_profile(path)
    return {"path": str(path), "overrides": merged, "summary": badvocab.summary()}
