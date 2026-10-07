"""S4 semantic selection: complete coverage, one representative per equivalent group."""
from __future__ import annotations

import json
from typing import Any

from .errors import AIReturnError

GROUP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"groups": {
        "type": "array", "minItems": 1,
        "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "fact": {"type": "string", "minLength": 1},
                "ids": {"type": "array", "minItems": 1,
                        "items": {"type": "integer", "minimum": 0}},
                "keep_id": {"type": "integer", "minimum": 0},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["fact", "ids", "keep_id", "reason"],
        },
    }},
    "required": ["groups"],
}


def group_prompt(candidates: list[dict[str, Any]], *, review: bool = False) -> str:
    payload = [{"id": c["id"], "text": c["text"],
                "seconds": round(float(c["end"]) - float(c["start"]), 3)}
               for c in candidates]
    task = "检查已编排全文的跨段落语义重复" if review else "对全部候选句单元做全局语义分组筛选"
    return (
        f"直接分析并输出JSON，不调用工具。任务：{task}。\n"
        "按具体对象、具体事实、数值与新增信息分组，不预设或固定商品名称。"
        "只有表达同一信息、没有实质新增内容的句单元才能合组；每组保留最佳表达一条。"
        "例如百分百羊毛/纯羊毛是同义；羊毛含量与不扎皮肤不是；"
        "可拆领子与可拆袖口不是。不同商品、不同版本、不同事实数值不能合并，"
        "有数值冲突时保持分开并在reason说明冲突，不能擅自判定真实值。\n"
        "最佳表达优先完整、具体、可独立听懂、没有场控夹杂；同等信息选简洁的。"
        "空泛好看/高级/舒服的同义夸赞也需去重，不能因分段角色不同就重复保留。"
        "含有独特新增信息的混合句单独成组，禁止为了去重丢失新信息或改写原话。"
        "不能确定等价时单独成组，不按材质/版型等大类粗暴合并。\n"
        "每个输入id必须恰好出现一次，包括单条组；禁止新增、遗漏、重复id。"
        "每组输出fact、ids、keep_id、reason，keep_id必须属于该组。"
        "不为目标时长保留同义句，不新增或补选内容。\n"
        f"句单元（JSON）：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def validate_groups(data: dict[str, Any], candidates: list[dict[str, Any]]
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise AIReturnError("S4 语义分组缺少非空 groups")
    expected = {c["id"] for c in candidates}
    seen: set[int] = set()
    kept: set[int] = set()
    deletions = []
    for group in groups:
        if not isinstance(group, dict):
            raise AIReturnError("S4 语义分组不是对象")
        ids, keep = group.get("ids"), group.get("keep_id")
        if not isinstance(ids, list) or not ids:
            raise AIReturnError("S4 语义分组 ids 为空")
        for cid in ids:
            if type(cid) is not int or cid not in expected or cid in seen:
                raise AIReturnError("S4 语义分组含越界或重复 id")
            seen.add(cid)
        if type(keep) is not int or keep not in ids:
            raise AIReturnError("S4 语义分组 keep_id 不属于该组")
        if any(not isinstance(group.get(key), str) or not group[key].strip()
               for key in ("fact", "reason")):
            raise AIReturnError("S4 语义分组缺少事实或选择理由")
        kept.add(keep)
        deletions.extend({"id": cid, "keep_id": keep, "fact": group["fact"],
                          "reason": group["reason"]} for cid in ids if cid != keep)
    if seen != expected:
        raise AIReturnError(f"S4 语义分组遗漏 id：{sorted(expected - seen)}")
    return ([c for c in candidates if c["id"] in kept],
            {"groups": groups, "deletions": deletions,
             "input_count": len(candidates), "kept_count": len(kept)})


def select_semantic(candidates, model, timeout, call, *, review=False):
    if not candidates:
        raise AIReturnError("S4 没有候选句单元")
    return validate_groups(call(model, group_prompt(candidates, review=review),
                                GROUP_SCHEMA, timeout), candidates)
