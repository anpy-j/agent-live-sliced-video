# -*- coding: utf-8 -*-
"""S2 规则粗筛（确定性）。

复用 ``badvocab``（违禁词/价格/库存/场控/绝对化/仿品货源等硬禁与正则），
外加时长/长度门禁、中文字符占比门禁，以及 normalize 后的近似去重。
命中即写 ``usable=False``、``reason=<规则名>``。

只做高置信度的确定性剔除；含糊的语义判断留给 S3。这里**不**因为
context_dependent_start / incomplete_sentence 就淘汰（子句已经保证不跨句，
残句由 S3 判），因此不会退化成旧的本地启发式兜底。

``badvocab`` 的账号层（``hard_add`` 等）需要显式加载；管线入口调用
:func:`activate_account_vocab`，命中即 ``hard_vocab``。该函数只影响进程内
``badvocab`` 全局词表，**改动需重启服务后才生效**。
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from agent_video.engine.scripts import badvocab, textnorm
from agent_video.engine.scripts.prep import is_cjk

DEFAULT_MIN_DURATION = 0.8
DEFAULT_MAX_SIMILARITY = 0.9
_NORMALIZE_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")

# 显式关闭账号词表（只用通用基础层）的取值。
_PROFILE_OFF = {"none", "off", "no", "0"}


def activate_account_vocab(path: str | None = None) -> str | None:
    """让 S2 使用账号级红线词表（默认 ``profiles/douyin-strict.json``）。

    精简管线此前只加载 ``badvocab`` 的通用基础层，账号词表里的 ``hard_add``
    （价格/现货/发货/库存/尺码/优惠券…）从未生效。这里在管线启动时显式加载账号
    词表：``_rule_reason`` 始终通过 ``badvocab.BAD_RE`` 动态取用，加载后立即生效。

    ``path`` 非空时使用它；显式传 ``none``/``off``/空串则回退到只用通用基础词表。
    未传 ``path`` 时：已有生效词表则沿用（不覆盖标注补丁），否则加载默认账号词表。
    返回实际生效的词表路径，未加载任何账号词表时返回 ``None``。本函数幂等。
    """
    if path is not None:
        normalized = str(path).strip()
        if normalized.lower() in _PROFILE_OFF or normalized == "":
            badvocab.set_profile({}, None)
            return None
        return badvocab.reload_profile(normalized).get("profile")
    if badvocab.PROFILE_SOURCE:
        return badvocab.PROFILE_SOURCE
    default = badvocab.default_profile_path()
    if not default:
        return None
    return badvocab.reload_profile(default).get("profile")


def normalize(text: str) -> str:
    """去掉标点/空白/大小写差异，只留可比较的字符。"""
    return _NORMALIZE_RE.sub("", text or "").lower()


def _rule_reason(clause: dict[str, Any], min_duration: float) -> str | None:
    text = str(clause.get("text") or "")
    if len(text.strip()) < 2:
        return "too_short"
    cjk = sum(1 for char in text if is_cjk(char))
    if cjk == 0:
        return "non_chinese"
    if cjk / max(1, len(text)) < 0.5:
        return "non_chinese"
    if badvocab.BAD_RE.search(text):
        return "hard_vocab"
    rejection = textnorm.content_rejection(text)
    if rejection == "stage_chatter":
        return "stage_chatter"
    if rejection == "malformed_speech":
        return "malformed_speech"
    try:
        if float(clause["end"]) - float(clause["start"]) < min_duration - 1e-9:
            return "duration_gate"
    except (KeyError, TypeError, ValueError):
        return "invalid_bounds"
    return None


def filter_clauses(clauses: list[dict[str, Any]],
                   min_duration: float = DEFAULT_MIN_DURATION,
                   max_similarity: float = DEFAULT_MAX_SIMILARITY) -> list[dict[str, Any]]:
    """就地写 ``usable``/``reason`` 并返回同一列表。"""
    for clause in clauses:
        clause["usable"] = True
        clause["reason"] = ""
    for clause in clauses:
        reason = _rule_reason(clause, min_duration)
        if reason:
            clause["usable"] = False
            clause["reason"] = reason

    kept: list[str] = []
    for clause in clauses:
        if not clause["usable"]:
            continue
        key = normalize(str(clause.get("text") or ""))
        duplicate = False
        for previous in kept:
            if key and (key == previous
                        or difflib.SequenceMatcher(None, key, previous).ratio()
                        >= max_similarity):
                duplicate = True
                break
        if duplicate:
            clause["usable"] = False
            clause["reason"] = "duplicate"
        else:
            kept.append(key)
    return clauses


def reject_hit(clause: dict[str, Any]) -> str | None:
    """返回导致淘汰的**字面命中**（可回填词表），结构性命中返回 None。

    只有 ``hard_vocab`` 的命中来自可配置词表；话术/病句命中来自 textnorm
    内置正则，不能靠词表增删，返回 None 以便标注端标为「无法自动成规」。
    """
    if str(clause.get("reason") or "") != "hard_vocab":
        return None
    return badvocab.hit(str(clause.get("text") or ""))
