# -*- coding: utf-8 -*-
"""S1 子句切分（确定性）。

输入是 whisper 的词级序列（``{s, e, w}``）。在词序列上按优先级切边界：

1. 句末标点 ``。！？!?…`` 之后；
2. 静音间隔 ``>= 0.30s``；
3. 强制上限 ``6.0s``。

最短 ``1.0s``：更短的孤片并入相邻子句（不允许跨句末标点合并，因此不会把多句
并成一段）。产出子句以 2–5s 为主。

旧实现 ``prep.safe_split_utterance`` 只在 >10s 时才切，粒度太粗；这里不依赖句级
分段，直接在词序列上工作，且不再做任何语义兜底。
"""
from __future__ import annotations

from typing import Any, Iterable

from agent_video.engine.scripts.prep import simp

FINAL_PUNCT = "。！？!?…"
DEFAULT_MAX_DURATION = 6.0
DEFAULT_MIN_DURATION = 1.0
DEFAULT_SILENCE_GAP = 0.30


def _token(word: dict[str, Any]) -> dict[str, Any] | None:
    text = str(word.get("w") or "").strip()
    if not text:
        return None
    try:
        start = float(word["s"])
        end = float(word["e"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    return {"s": start, "e": end, "w": text}


def _ends_sentence(token: dict[str, Any]) -> bool:
    text = token["w"]
    return bool(text) and text[-1] in FINAL_PUNCT


def _clause_text(tokens: Iterable[dict[str, Any]]) -> str:
    return simp("".join(token["w"] for token in tokens).strip())


def _merge_short(groups: list[list[dict]], min_duration: float) -> list[list[dict]]:
    """把 <min_duration 的孤片并入相邻子句；不跨句末标点。"""

    def duration(group: list[dict]) -> float:
        return group[-1]["e"] - group[0]["s"]

    first: list[list[dict]] = []
    for group in groups:
        if (first and duration(group) < min_duration - 1e-9
                and not _ends_sentence(first[-1][-1])):
            first[-1].extend(group)
        else:
            first.append(list(group))

    # 前导的短孤片没有「上一条」可并，这里把它并入下一条。
    second: list[list[dict]] = []
    for group in first:
        if (second and duration(second[-1]) < min_duration - 1e-9
                and not _ends_sentence(second[-1][-1])):
            second[-1].extend(group)
        else:
            second.append(list(group))
    return second


def _split_from(clauses: list[dict], sentences: list[dict] | None) -> None:
    """标记确实被从一句里切出来的子句来源句序号，否则保持 null。"""

    if not sentences:
        return
    spans = []
    for index, sentence in enumerate(sentences):
        try:
            spans.append((index, float(sentence["start"]), float(sentence["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    counts: dict[int, int] = {}
    owners: list[int | None] = []
    for clause in clauses:
        midpoint = (clause["start"] + clause["end"]) / 2.0
        owner = None
        for index, start, end in spans:
            if start - 1e-6 <= midpoint <= end + 1e-6:
                owner = index
                break
        owners.append(owner)
        if owner is not None:
            counts[owner] = counts.get(owner, 0) + 1
    for clause, owner in zip(clauses, owners):
        clause["split_from"] = owner if owner is not None and counts.get(owner, 0) > 1 else None


def split_clauses(words: list[dict[str, Any]],
                  sentences: list[dict[str, Any]] | None = None,
                  max_duration: float = DEFAULT_MAX_DURATION,
                  min_duration: float = DEFAULT_MIN_DURATION,
                  silence_gap: float = DEFAULT_SILENCE_GAP) -> list[dict[str, Any]]:
    """把词级序列切成子句数组。"""
    tokens = [token for token in (_token(word) for word in words) if token]
    tokens.sort(key=lambda token: (token["s"], token["e"]))

    groups: list[list[dict]] = []
    current: list[dict] = []
    for index, token in enumerate(tokens):
        if current and token["e"] - current[0]["s"] > max_duration + 1e-9:
            groups.append(current)
            current = []
        current.append(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if _ends_sentence(token):
            groups.append(current)
            current = []
        elif following is not None and following["s"] - token["e"] >= silence_gap - 1e-9:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    groups = _merge_short(groups, min_duration)

    clauses: list[dict[str, Any]] = []
    for group in groups:
        text = _clause_text(group)
        if not text:
            continue
        clauses.append({
            "id": len(clauses),
            "start": round(group[0]["s"], 3),
            "end": round(group[-1]["e"], 3),
            "text": text,
            "usable": None,
            "reason": "",
            "order": None,
            "split_from": None,
        })
    _split_from(clauses, sentences)
    return clauses
