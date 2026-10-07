"""Structured S4 composition and bounded, resumable S5 whole-script review."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

from .ai import ORDER_SCHEMA
from .errors import AIReturnError

TOPICS = ["fabric", "fit", "wearing", "styling", "color", "care", "other"]
def array(item):
    return {"type": "array", "items": item}
def obj(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}
INT = {"type": "integer", "minimum": 0}
STR = {"type": "string", "minLength": 1}
INVENTORY_SCHEMA = obj({"candidates": array(obj({
    "id": INT, "topic": {"type": "string", "enum": TOPICS},
    "facts": array(STR), "subject": STR, "eligible": {"type": "boolean"},
    "requires": array(INT), "reason": STR,
}))})
PLAN_SCHEMA = copy.deepcopy(ORDER_SCHEMA)
PLAN_SCHEMA["properties"]["opening_topic"] = {"type": "string", "enum": TOPICS}
PLAN_SCHEMA["required"].append("opening_topic")
section_schema = PLAN_SCHEMA["properties"]["sections"]["items"]
section_schema["properties"]["topic"] = {"type": "string", "enum": TOPICS}
section_schema["required"].append("topic")
REVIEW_SCHEMA = obj({"passed": {"type": "boolean"}, "issues": array(obj({
    "ids": array(INT), "kind": STR, "reason": STR,
    "action": {"type": "string", "enum": ["drop", "replace", "repair", "enrich"]},
}))})

def save(path, data):
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")
    tmp.replace(target)

def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()

def payload(candidates):
    return [{"id": c["id"], "text": c["text"],
             "seconds": round(c["end"] - c["start"], 3)} for c in candidates]

def inventory(candidates, model, timeout, call, *, context=None):
    prompt = (
        "分析全部S3候选，不固定商品名称，不改写原话。为每个id给出结构化信息标注。"
        "eligible只放行有独立信息价值且可用于本片的表达；纯夸赞、Sales/大货故事、"
        "名词罗列、场控、报价、无对象保证、数值冲突应排除。识别本品/对比品/旧款，"
        "对比必须完整明确。facts用具体对象+具体事实的全局统一键，同一事实不同说法"
        "必须用相同键；材质比例与保暖原理是不同事实。数值冲突不自行定真假。"
        "topic为fabric面料、fit版型、wearing上身感受、styling搭配、color颜色、"
        "care养护或other。主主题只能一个，混合句谨慎处理。requires列出不可分离的上下文补全句id："
        "当前句所需的前句或后句id，依赖组保持原素材时间顺序，不把前句移到后面；独立完整句为空。"
        "每个输入id恰好标注一次，不得新增或遗漏。直接返回紧凑JSON，不写脚本、不调用工具。"
        "reason用一句短语。上下文仅供判断依赖，不输出上下文的标注。\n候选JSON：\n"
        + json.dumps(payload(candidates), ensure_ascii=False))
    if context:
        prompt += '\n上下文JSON：\n' + json.dumps(payload(context), ensure_ascii=False)
    data = call(model, prompt, INVENTORY_SCHEMA, timeout)
    rows = data.get("candidates")
    expected = {c["id"] for c in candidates}
    dependency_ids = expected | {c['id'] for c in (context or [])}
    seen = set()
    if not isinstance(rows, list):
        raise AIReturnError("S4 信息标注缺少 candidates")
    for row in rows:
        if not isinstance(row, dict):
            raise AIReturnError("S4 信息标注不是对象")
        cid = row.get("id")
        if type(cid) is not int or cid not in expected or cid in seen:
            raise AIReturnError("S4 信息标注ID重复或越界")
        seen.add(cid)
        if (row.get("topic") not in TOPICS or type(row.get("eligible")) is not bool
                or not isinstance(row.get("facts"), list)
                or any(not isinstance(f, str) or not f.strip() for f in row["facts"])
                or not isinstance(row.get("requires"), list)
                or any(type(x) is not int or x not in dependency_ids or x == cid
                       for x in row["requires"])
                or any(not isinstance(row.get(k), str) or not row[k].strip()
                       for k in ("subject", "reason"))):
            raise AIReturnError("S4 信息标注字段非法")
        if row["eligible"] and not row["facts"]:
            raise AIReturnError("S4 可入选句必须提供新增事实")
    if seen != expected:
        raise AIReturnError("S4 信息标注遗漏候选")
    return {row["id"]: row for row in rows}

def dependency_blocks(candidates, labels, excluded=()):
    """Resolve complete dependency units; reject unusable, cyclic or mixed-topic units."""
    by_id = {c['id']: c for c in candidates}
    blocked = set(excluded)
    result = {}
    def visit(cid, stack, found):
        if cid in stack:
            raise AIReturnError(f'S4 依赖循环：{cid}')
        if cid not in by_id or cid not in labels or cid in blocked or not labels[cid]['eligible']:
            raise AIReturnError(f'S4 依赖不可用：{cid}')
        if cid in found:
            return
        stack.add(cid)
        for dep in labels[cid]['requires']:
            visit(dep, stack, found)
        stack.remove(cid)
        found.add(cid)
    for cid in by_id:
        found = set()
        try:
            visit(cid, set(), found)
            if len({labels[x]['topic'] for x in found}) != 1:
                raise AIReturnError('S4 依赖跨主题')
            facts = [f for x in found for f in labels[x]['facts']]
            if len(facts) != len(set(facts)):
                raise AIReturnError('S4 依赖组事实重复')
            result[cid] = sorted(found, key=lambda x: (by_id[x]['start'], by_id[x]['end'], x))
        except AIReturnError:
            continue
    return result


def complete_dependencies(plan, candidates, labels, excluded=()):
    blocks = dependency_blocks(candidates, labels, excluded)
    by_id = {c['id']: c for c in candidates}
    selected = plan.get('ordered_ids', [])
    groups = []
    for cid in selected:
        if cid not in blocks:
            raise AIReturnError(f'S4 选中不可成立的依赖组：{cid}')
        group = set(blocks[cid])
        overlapping = [g for g in groups if g & group]
        for g in overlapping:
            group.update(g)
            groups.remove(g)
        groups.append(group)
    ordered_groups = {cid: sorted(g, key=lambda x: (by_id[x]['start'], by_id[x]['end'], x))
                      for g in groups for cid in g}
    revised = copy.deepcopy(plan)
    emitted, sections = set(), []
    for section in revised['sections']:
        ids = []
        for cid in section['ids']:
            if cid in emitted:
                continue
            block = ordered_groups[cid]
            ids.extend(block)
            emitted.update(block)
        if ids:
            sections.append(dict(section, ids=ids))
    revised['sections'] = sections
    revised['ordered_ids'] = [cid for s in sections for cid in s['ids']]
    if revised['ordered_ids'] != selected:
        revised['dependency_repair'] = {'before': selected, 'after': revised['ordered_ids']}
    return revised


def validate_plan(plan, candidates, labels, target, tolerance, validate):
    main, ids, seconds, _ = validate(plan, candidates, target, tolerance)
    for c in candidates:
        if not math.isfinite(c["end"] - c["start"]) or c["end"] <= c["start"]:
            raise AIReturnError("S4 候选时间范围非法")
    if plan.get("opening_topic") not in TOPICS:
        raise AIReturnError("S4 缺少开头主题")
    seen_topics, seen_facts = set(), set()
    previous = None
    for section in plan["sections"]:
        topic = section.get("topic")
        if topic not in TOPICS or (topic != previous and topic in seen_topics):
            raise AIReturnError("S4 主题回跳或主题非法")
        if previous is None and topic != plan["opening_topic"]:
            raise AIReturnError("S4 开头与首块主题不一致")
        seen_topics.add(topic)
        previous = topic
        for cid in section["ids"]:
            label = labels[cid]
            if not label["eligible"] or topic != label["topic"]:
                raise AIReturnError("S4 选入低价值句或主题错位")
            facts = set(label["facts"])
            if facts & seen_facts:
                raise AIReturnError("S4 同一具体事实重复入选")
            seen_facts.update(facts)
    blocks = dependency_blocks(candidates, labels)
    for cid in ids:
        if cid not in blocks or not set(blocks[cid]).issubset(ids):
            raise AIReturnError(f"S4 缺失或不可用的依赖句：{cid}")
    # Merge shared context into one contiguous unit, ordered by original source time.
    completed = complete_dependencies(plan, candidates, labels)
    if completed['ordered_ids'] != ids:
        raise AIReturnError('S4 依赖组未相邻或原时间顺序错误')
    plan["total_seconds"] = round(seconds, 3)
    plan["main_product"] = main
    return plan

def compose(candidates, labels, model, timeout, call, target, tolerance, validate,
            *, current=None, issues=None, excluded=()):
    blocks = dependency_blocks(candidates, labels, excluded)
    # Equivalent complete units need only one representative in the model input.
    # Never collapse complementary facts or split a dependency unit.
    by_id = {c['id']: c for c in candidates}
    representatives = {}
    for cid, block in blocks.items():
        signature = (labels[cid]['topic'], tuple(sorted(
            {fact for member in block for fact in labels[member]['facts']})))
        score = (sum(by_id[member]['end'] - by_id[member]['start'] for member in block), cid)
        previous = representatives.get(signature)
        if previous is None or score < previous[0]:
            representatives[signature] = (score, block)
    retained = {cid for _, block in representatives.values() for cid in block}
    available = [dict(c, annotation={k: labels[c['id']][k] for k in ('topic', 'facts', 'requires')},
                      dependency_block=blocks[c['id']])
                 for c in payload(candidates) if c['id'] in retained]
    prompt = (
        "S4精选编排：从候选选值得讲的信息与最佳完整表达，不生成新话术。"
        "先回答是什么、为什么有用、适合谁、怎么穿。开头选具体痛点/利益，后续"
        "接同一主题讲完再换主题。同主题连续成块，禁止面料→颜色→面料回跳。"
        "同一facts键只选一次，保留新增事实；dependency_block是程序算好的完整依赖组，"
        "整组选取，按给定的原素材顺序连续播放，依赖可以在前或在后。"
        "sections保留hook/scene/selling_point/proof/styling/cta角色，但每段必须"
        "同时有topic；opening_topic等于首段主题。hook只一个，CTA仅最后。"
        f"目标{target[0]:g}–{target[1]:g}秒。删后在范围且完整就只删不补；"
        "低于下限才补同主题的新信息，已耗尽有效信息时允许短片，不凑废话。"
        "语义不完整时即使时长达标也要替换或补全，但控制上限。"
        "修订只改问题区域，保留无问题内容，不能重选excluded的句子。\n"
        + json.dumps({"candidates": available, "current": current,
                      "issues": issues or [], "excluded": list(excluded), "target": target}, ensure_ascii=False))
    for attempt in range(2):
        plan = call(model, prompt, PLAN_SCHEMA, timeout)
        try:
            # Check the AI's raw IDs/roles before deterministic additions and reordering.
            validate(plan, candidates, target, tolerance)
            if set(plan.get('ordered_ids', [])) - set(blocks):
                raise AIReturnError('S4 选入不可用依赖组或已淘汰句')
            plan = complete_dependencies(plan, candidates, labels, excluded)
            if current:
                plan['stage'] = 'order'
                plan['semantic_summary'] = current.get('semantic_summary', {})
            return validate_plan(plan, candidates, labels, target, tolerance, validate)
        except AIReturnError as exc:
            if attempt == 1:
                raise
            prompt = '上次方案未通过程序校验，请替换问题组合并重新编排：' + str(exc) + '\n' + prompt
    raise AIReturnError('S4 未生成有效方案')

def review_and_revise(plan, candidates, labels, model, timeout, call, target,
                      tolerance, validate, workdir):
    """At most three review attempts, including errors; no repair after the third."""
    path = Path(workdir) / "review.json"
    source_hash = digest({"candidates": candidates, "labels": labels, "target": target})
    state = {"source_hash": source_hash, "candidate_hash": digest(candidates),
             "attempts": [], "plan": plan, "excluded": []}
    validate_plan(plan, candidates, labels, target, tolerance, validate)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf8"))
        if (old.get("source_hash") == source_hash
                and digest(plan) in {digest(old.get("plan")), old.get("previous_plan_hash")}):
            state = old
    by_id = {c["id"]: c for c in candidates}
    while len(state["attempts"]) < 3 and not state.get("released"):
        attempt = {"number": len(state["attempts"]) + 1, "plan_hash": digest(state["plan"])}
        state["attempts"].append(attempt)
        save(path, state)  # Persist budget before a potentially interrupted call.
        script = [dict(by_id[cid], annotation=labels[cid]) for cid in state["plan"]["ordered_ids"]]
        prompt = (
            "S5正片文本筛查：按播放顺序一次看完整文本，不孤立逐句判。"
            "检查通顺、开头承接、重复、残句、指代对象、因果问答、主题回跳、"
            "空泛信息和事实冲突。返回问题ids、kind、reason、action；"
            "drop删除后无需补、replace换完整表达、repair修复承接顺序、"
            "enrich仅用于低于目标下限补新增信息。没有问题才passed=true且issues为空。"
            "不改写原话，不把旧款口碑误作本品。\n"
            + json.dumps({"plan": state["plan"], "script": script,
                          "target": target}, ensure_ascii=False))
        try:
            result = call(model, prompt, REVIEW_SCHEMA, timeout)
            issues = result.get("issues")
            if type(result.get("passed")) is not bool or not isinstance(issues, list):
                raise AIReturnError("S5 返回格式非法")
            for issue in issues:
                if (not isinstance(issue, dict) or not isinstance(issue.get("ids"), list)
                        or any(type(cid) is not int or cid not in state["plan"]["ordered_ids"]
                               for cid in issue["ids"])
                        or issue.get("action") not in {"drop", "replace", "repair", "enrich"}
                        or any(not isinstance(issue.get(k), str) or not issue[k].strip()
                               for k in ("kind", "reason"))):
                    raise AIReturnError("S5 问题字段非法")
            if result["passed"] != (not issues):
                raise AIReturnError("S5 通过标记与问题列表矛盾")
            attempt["review"] = result
            remaining = [cid for cid, label in labels.items()
                         if label["eligible"] and cid not in state["plan"]["ordered_ids"]
                         and cid not in state["excluded"]]
            used_facts = {f for cid in state["plan"]["ordered_ids"] for f in labels[cid]["facts"]}
            remaining = [cid for cid in remaining if not used_facts.intersection(labels[cid]["facts"])]
            if result["passed"] and state["plan"]["total_seconds"] < target[0] and remaining:
                issues = [{"ids": [], "kind": "duration", "reason": "低于下限且仍有新增信息候选", "action": "enrich"}]
                attempt["duration_repair"] = True
                attempt["effective_issues"] = issues
                result = {"passed": False, "issues": issues}
            if result["passed"]:
                state["released"] = "passed"
                break
            if len(state["attempts"]) == 3:
                break
            excluded = set(state["excluded"])
            excluded.update(cid for i in issues if i["action"] in {"drop", "replace"} for cid in i["ids"])
            state["excluded"] = sorted(excluded)
            revised = None
            # drop means the reviewer found removal semantically complete. Keep every
            # unaffected sentence when the remaining duration already meets the range.
            if issues and all(i["action"] == "drop" for i in issues):
                deleted = copy.deepcopy(state["plan"])
                deleted["ordered_ids"] = [cid for cid in deleted["ordered_ids"] if cid not in excluded]
                for section in deleted["sections"]:
                    section["ids"] = [cid for cid in section["ids"] if cid not in excluded]
                deleted["sections"] = [s for s in deleted["sections"] if s["ids"]]
                try:
                    validate_plan(deleted, candidates, labels, target, tolerance, validate)
                    if target[0] <= deleted["total_seconds"] <= target[1]:
                        revised = deleted
                        attempt["repair"] = "delete_only"
                except AIReturnError:
                    pass
            if revised is None:
                attempt["repair"] = "compose"
                revised = compose(candidates, labels, model, timeout, call, target, tolerance,
                                  validate, current=state["plan"], issues=issues, excluded=excluded)
            state["previous_plan_hash"] = digest(state["plan"])
            state["plan"] = revised
            save(path, state)
            save(Path(workdir) / "order.json", revised)
        except Exception as exc:
            attempt["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            save(path, state)
    state["released"] = state.get("released") or "limit_released"
    validate_plan(state["plan"], candidates, labels, target, tolerance, validate)
    state["approved_hash"] = digest(state["plan"])
    save(path, state)
    save(Path(workdir) / "order.json", state["plan"])
    return state

def require_release(plan, workdir, candidates=None):
    path = Path(workdir) / "review.json"
    if not path.exists():
        raise AIReturnError("缺少S5筛查记录，请先执行正片文本筛查")
    state = json.loads(path.read_text(encoding="utf8"))
    if state.get("released") not in {"passed", "limit_released"} or state.get("approved_hash") != digest(plan):
        raise AIReturnError("S5筛查记录与当前成片方案不一致")
    if candidates is not None and state.get("candidate_hash") != digest(candidates):
        raise AIReturnError("S5筛查后候选文本或时间范围发生变化")
    return state
