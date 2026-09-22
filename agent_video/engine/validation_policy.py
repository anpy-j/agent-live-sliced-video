"""Shared deterministic policy used by AI preflight and final timeline validation."""
from __future__ import annotations

import difflib
import re
from typing import Any

from .scripts.dependency_graph import dependency_issues

MIN_SEGMENT_SECONDS = 1.2
MAX_SEGMENT_SECONDS = 5.0
# 不可切分的原生完整长句才允许突破 5 秒；旧值 18 秒让编排用长句凑段数，
# 产出「90 秒 10 段」这种不符合 2-5 秒片段口径的成片。
MAX_LONG_COMPLETE_SECONDS = 8.0
ALIGNMENT_EXPANSION_MARGIN = 0.30
MAX_DEMOS = 3
MAX_MATERIAL_SEGMENTS = 1


def normalized(text: str) -> str:
    return "".join(char.lower() for char in text
                   if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _near_duplicate(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    left_pairs = {left[index:index + 2] for index in range(max(0, len(left) - 1))}
    right_pairs = {right[index:index + 2] for index in range(max(0, len(right) - 1))}
    overlap = len(left_pairs & right_pairs) / max(1, len(left_pairs | right_pairs))
    return min(len(left), len(right)) >= 6 and (ratio >= 0.84 or overlap >= 0.30)


def shared_issues(rows: list[dict[str, Any]], *, pre_alignment: bool = False) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    issues.extend(dependency_issues(rows))
    maximum = MAX_SEGMENT_SECONDS - (ALIGNMENT_EXPANSION_MARGIN if pre_alignment else 0)
    texts: list[tuple[int, str]] = []
    material: dict[str, list[int]] = {}
    demos = []
    for index, row in enumerate(rows):
        duration = float(row.get("end", 0)) - float(row.get("start", 0))
        if duration < MIN_SEGMENT_SECONDS - 1e-6:
            issues.append({"level": "error", "code": "segment_too_short", "segment": index,
                           "detail": f"第 {index + 1} 段 {duration:.2f}s 过短"})
        is_long_complete = bool(row.get("long_complete_utterance"))
        seg_max = MAX_LONG_COMPLETE_SECONDS if is_long_complete else maximum
        if duration > seg_max + 1e-6:
            tolerated = (MAX_LONG_COMPLETE_SECONDS if is_long_complete else
                         (MAX_SEGMENT_SECONDS if pre_alignment
                          else MAX_SEGMENT_SECONDS + ALIGNMENT_EXPANSION_MARGIN))
            level = "warning" if duration <= tolerated + 1e-6 else "error"
            issues.append({"level": level, "code": "segment_too_long", "segment": index,
                           "detail": f"第 {index + 1} 段 {duration:.2f}s 超过 {seg_max:.2f}s"})
        role = str(row.get("role", ""))
        if role == "material" or re.search(r"面料|材质|成分|羊毛|醋酸", str(row.get("text", ""))):
            product = str(row.get("product") or "__whole_video__").strip()
            material.setdefault(product, []).append(index)
        if role == "demo":
            demos.append(index)
        norm = normalized(str(row.get("text", "")))
        for other_index, other in texts:
            if _near_duplicate(norm, other):
                issues.append({"level": "error", "code": "duplicate_text", "segments": [other_index, index],
                               "detail": f"第 {other_index + 1}/{index + 1} 段语义重复"})
                break
        texts.append((index, norm))
    for product, indexes in material.items():
        if len(indexes) > MAX_MATERIAL_SEGMENTS:
            label = "完整成片" if product == "__whole_video__" else f"商品“{product}”"
            issues.append({"level": "error", "code": "too_many_material_segments",
                           "segments": indexes,
                           "detail": f"讲面料不要过多；{label}面料内容最多出现一次"})
    if len(demos) > MAX_DEMOS:
        issues.append({"level": "warning", "code": "too_many_long_demos", "segments": demos,
                       "detail": "连续展示较多；确认每段动作或效果确有新增信息"})
    return issues
