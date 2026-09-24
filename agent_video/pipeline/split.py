# -*- coding: utf-8 -*-
"""S1 子句切分（确定性）。

输入是 whisper 的词级序列（``{s, e, w}``）与句级分段。切分边界按优先级：

1. **ASR 句级分段边界**（有 ``sentences`` 时）——这是 whisper 依据停顿切出的
   自然句，是硬切点，永不跨句切词；
2. 句末标点 ``。！？!?…`` 之后（句内也切）；
3. 无 ``sentences`` 时退回：静音间隔 ``>= 0.30s``。

``6.0s`` 上限不再是「到点就砍」——只有单句本身超长时才切，且优先切在句读/停顿处、
靠中点最近的边界，避免把词从中间劈开（旧实现会在 6.0s 处硬砍，产生
「不要高温高 / 温洗羊毛会洗坏的」这类断词）。

最短 ``1.0s``：更短的孤片并入相邻子句，合并后不得超过上限，也不跨句末标点。
"""
from __future__ import annotations

from typing import Any, Iterable

from agent_video.engine.scripts.prep import simp

FINAL_PUNCT = "。！？!?…"
CLAUSE_PUNCT = "，,、；;：:"
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


def _sentence_spans(sentences: list[dict[str, Any]] | None) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for sentence in sentences or []:
        try:
            start = float(sentence["start"])
            end = float(sentence["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end))
    spans.sort(key=lambda span: span[0])
    return spans


def _owner(token: dict[str, Any], spans: list[tuple[float, float]]) -> int | None:
    midpoint = (token["s"] + token["e"]) / 2.0
    for index, (start, end) in enumerate(spans):
        if start - 1e-6 <= midpoint <= end + 1e-6:
            return index
    return None


def _group_tokens(tokens: list[dict[str, Any]],
                  spans: list[tuple[float, float]],
                  silence_gap: float) -> list[list[dict[str, Any]]]:
    """按句级分段 / 句末标点（无句级分段时加静音）切出基础组。"""
    owners = [_owner(token, spans) for token in tokens] if spans else None
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_owner: int | None = None
    for index, token in enumerate(tokens):
        owner = owners[index] if owners is not None else None
        if current and owners is not None and owner != current_owner:
            groups.append(current)
            current = []
        if not current:
            current_owner = owner
        current.append(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if _ends_sentence(token):
            groups.append(current)
            current = []
        elif (following is not None and owners is None
                and following["s"] - token["e"] >= silence_gap - 1e-9):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _cut_index(group: list[dict[str, Any]], max_duration: float) -> int:
    """单句超长时选切点：贴近中点，优先句读 / 停顿，且左片不超上限。"""
    midpoint = (group[0]["s"] + group[-1]["e"]) / 2.0
    best: int | None = None
    best_score: float | None = None
    for index in range(1, len(group)):
        if group[index]["s"] - group[0]["s"] > max_duration + 1e-9:
            continue
        previous = group[index - 1]
        gap = group[index]["s"] - previous["e"]
        score = abs(group[index]["s"] - midpoint)
        if previous["w"] and previous["w"][-1] in CLAUSE_PUNCT:
            score -= 0.5
        if gap >= 0.15:
            score -= 0.3
        if best_score is None or score < best_score:
            best_score = score
            best = index
    if best is None:
        for index in range(len(group) - 1, 0, -1):
            if group[index]["s"] - group[0]["s"] <= max_duration + 1e-9:
                best = index
                break
    return best if best is not None else max(1, len(group) // 2)


def _enforce_max(groups: list[list[dict[str, Any]]],
                 max_duration: float) -> list[list[dict[str, Any]]]:
    """把超长的基础组在句读/停顿处递归切开，不再从词中间硬砍。"""
    result: list[list[dict[str, Any]]] = []
    pending = list(groups)
    while pending:
        group = pending.pop(0)
        duration = group[-1]["e"] - group[0]["s"]
        if duration <= max_duration + 1e-9 or len(group) < 2:
            result.append(group)
            continue
        cut = _cut_index(group, max_duration)
        pending.insert(0, group[cut:])
        pending.insert(0, group[:cut])
    return result


def _merge_short(groups: list[list[dict]],
                 min_duration: float,
                 max_duration: float) -> list[list[dict]]:
    """把 <min_duration 的孤片并入相邻子句；不跨句末标点，不超上限。"""

    def duration(group: list[dict]) -> float:
        return group[-1]["e"] - group[0]["s"]

    def room(previous: list[dict], group: list[dict]) -> bool:
        return duration(previous) + duration(group) <= max_duration + 1e-9

    first: list[list[dict]] = []
    for group in groups:
        if (first and duration(group) < min_duration - 1e-9
                and not _ends_sentence(first[-1][-1])
                and room(first[-1], group)):
            first[-1].extend(group)
        else:
            first.append(list(group))

    # 前导的短孤片没有「上一条」可并，这里把它并入下一条。
    second: list[list[dict]] = []
    for group in first:
        if (second and duration(second[-1]) < min_duration - 1e-9
                and not _ends_sentence(second[-1][-1])
                and room(second[-1], group)):
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

    spans = _sentence_spans(sentences)
    groups = _group_tokens(tokens, spans, silence_gap)
    groups = _merge_short(groups, min_duration, max_duration)
    groups = _enforce_max(groups, max_duration)

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
