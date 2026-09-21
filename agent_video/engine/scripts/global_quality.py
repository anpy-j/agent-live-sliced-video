"""Deterministic adjacent-line and whole-copy quality gate.

The model may suggest an editorial order, but only source-backed rows reach this
gate.  It deliberately focuses on contradictions that can be proven locally;
ambiguous stylistic preferences remain warnings.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from .textnorm import context_dependent_start, incomplete_ending


PRONOUN_START = re.compile(
    r"^(?:这|这个|这种|这样|它|它们|那|那个|这些|那些)(?:个|件|款|种|样|条|套|双|色|版)?|"
    r"^(?:这(?:条|套|双)(?:的话)?|穿上以后|腰部这里|它整个就是|你看这个)"
)
CAUSE_START = re.compile(r"^(?:所以|因此|正因为|因为这样|这样一来)")
CONDITION_START = re.compile(r"^(?:如果|要是|只要|除非|当你|假如)")
LOW_VALUE_OPENING = re.compile(
    r"^(?:姐妹们|宝贝们|宝宝们|家人们|欢迎|能听到吗|看得到吗|"
    r"今天给大家|接下来给大家|这一款|这款是|我们来看一下)"
)
EMPTY_PRAISE_OPENING = re.compile(r"^(?:真的|非常|特别|超级|巨|太)?(?:好看|漂亮|高级|绝了|喜欢)[啊呀哦吧！!。]*$")
SHOUT_OPENING = re.compile(r"^(?:冲冲冲|上车|拍它|闭眼入|抢起来|不要犹豫)")


def _norm(value: str) -> str:
    return "".join(char.lower() for char in value
                   if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _near_duplicate(left: str, right: str) -> bool:
    left, right = _norm(left), _norm(right)
    return (min(len(left), len(right)) >= 6
            and difflib.SequenceMatcher(None, left, right).ratio() >= 0.86)


def review_copy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    information_chars = 0
    seen_products: set[str] = set()
    last_product = ""
    seen_colors: dict[str, set[str]] = {}
    last_color: dict[str, str] = {}
    for index, row in enumerate(rows):
        text = str(row.get("text") or "").strip()
        information_chars += len(_norm(text))
        previous = rows[index - 1] if index else None
        following = rows[index + 1] if index + 1 < len(rows) else None
        previous_atom = str((previous or {}).get("atom_id") or "")
        following_atom = str((following or {}).get("atom_id") or "")
        required = {str(value) for value in row.get("required_atom_ids") or []}
        requires_previous = bool(row.get("requires_previous") or
                                 context_dependent_start(text) or PRONOUN_START.match(text))
        if requires_previous and (not previous or (required and previous_atom not in required)):
            issues.append({"code": "dangling_reference", "level": "error",
                           "segment": index,
                           "detail": f"第 {index + 1} 句依赖的上文未紧邻绑定"})
        requires_next = bool(row.get("requires_next") or incomplete_ending(text))
        if requires_next and (not following or (required and following_atom not in required)):
            issues.append({"code": "dangling_continuation", "level": "error",
                           "segment": index,
                           "detail": f"第 {index + 1} 句依赖的下文未紧邻绑定"})
        ending = incomplete_ending(text)
        if ending and not requires_next:
            issues.append({"code": "unfinished_connector", "level": "error",
                           "segment": index, "detail": f"未完成连接词：{ending}"})
        if CONDITION_START.match(text) and not re.search(r"(?:就|那|会|可以|建议|适合)", text):
            issues.append({"code": "unfinished_condition", "level": "error",
                           "segment": index, "detail": "条件句没有可追溯的结论"})
        if CAUSE_START.match(text) and not previous:
            issues.append({"code": "unsupported_causality", "level": "error",
                           "segment": index, "detail": "因果结论前没有原因"})
        for previous_index, old in enumerate(rows[:index]):
            if _near_duplicate(text, str(old.get("text") or "")):
                issues.append({"code": "repeated_conclusion", "level": "error",
                               "segments": [previous_index, index],
                               "detail": "近义结论重复"})
                break

        product = str(row.get("product") or "").strip()
        color = str(row.get("color") or "").strip()
        if product and product != last_product:
            if product in seen_products:
                issues.append({"code": "product_jump", "level": "error",
                               "segment": index, "detail": f"商品“{product}”交叉跳回"})
            seen_products.add(product)
            last_product = product
        if product and color and color != last_color.get(product, ""):
            used = seen_colors.setdefault(product, set())
            if color in used:
                issues.append({"code": "color_jump", "level": "error",
                               "segment": index, "detail": f"颜色“{color}”交叉跳回"})
            used.add(color)
            last_color[product] = color

    opening_rows, elapsed = [], 0.0
    for row in rows:
        opening_rows.append(row)
        elapsed += max(0.0, float(row.get("end", 0)) - float(row.get("start", 0)))
        if elapsed >= 5.0:
            break
    opening_text = "".join(str(row.get("text") or "") for row in opening_rows)
    opening_roles = {str(row.get("role") or "") for row in opening_rows}
    # Length alone must never manufacture a "strong hook".  It contributes a
    # bounded clarity signal; concrete role and semantic evidence carry the rest.
    opening_score = min(45, round(len(_norm(opening_text)) * 1.2))
    role_bonus = {"result": 32, "pain": 30, "demo": 30, "reaction": 28,
                  "story": 27, "visual": 27, "hook": 25, "fit": 24,
                  "proof": 24, "personality": 22}
    opening_score += max((role_bonus.get(role, 0) for role in opening_roles), default=0)
    if LOW_VALUE_OPENING.match(opening_text):
        opening_score -= 45
    if EMPTY_PRAISE_OPENING.match(opening_text):
        opening_score -= 50
    if SHOUT_OPENING.match(opening_text):
        opening_score -= 55
    opening_score = max(0, min(100, opening_score))
    audited_opening_scores = [int(row.get("semantic_opening_suitability", 0))
                              for row in opening_rows
                              if "semantic_opening_suitability" in row]
    if audited_opening_scores:
        # Length and a model-assigned role are weak proxies for attraction.  When the
        # independent sentence audit is available, make its explicit opening judgment
        # dominant while retaining a small deterministic sanity component.
        opening_score = round(max(audited_opening_scores) * 0.65 + opening_score * 0.35)
        if LOW_VALUE_OPENING.match(opening_text) or EMPTY_PRAISE_OPENING.match(opening_text):
            opening_score = min(opening_score, 35)
        if SHOUT_OPENING.match(opening_text):
            opening_score = min(opening_score, 25)
    if opening_score < 45:
        issues.append({"code": "weak_opening_reason", "level": "warning", "segment": 0,
                       "detail": "前 3–5 秒缺少明确观看理由"})

    products = {str(row.get("product") or "").strip() for row in rows
                if str(row.get("product") or "").strip()}
    if not products:
        issues.append({"code": "unclear_main_product", "level": "error",
                       "detail": "整条文案没有可追溯的主商品标记"})
    density = round(information_chars / max(1.0, sum(
        max(0.0, float(row.get("end", 0)) - float(row.get("start", 0))) for row in rows)), 2)
    errors = [item for item in issues if item.get("level") == "error"]
    score = max(0, 100 - len(errors) * 18 - max(0, 45 - opening_score))
    return {"ok": not errors, "score": score, "opening_score": opening_score,
            "information_density": density, "main_products": sorted(products),
            "issues": issues}
