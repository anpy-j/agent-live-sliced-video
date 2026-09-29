# -*- coding: utf-8 -*-
"""精简管线编排 + 独立入口。

一条命令：ASR → 规则筛 → 2 次无状态 AI 判定/排序 → 按时间戳切 → ffmpeg 拼接。

    python -m agent_video.pipeline.run --media /abs/素材.mp4 --workdir /abs/out

唯一数据契约是 ``timeline.json``：子句数组，全程只往对象上写字段。失败按
``errors.PipelineError`` 的子类区分（规则筛空 / AI 报错 / 渲染失败），不做兜底。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable

from . import asr as asr_mod
from .ai import (DEFAULT_TIMEOUT, DECISION_SCHEMA, ORDER_SCHEMA,
                 ORDER_SECTION_ROLES, ai_call)
from .errors import (AIReturnError, AsrError, PipelineError, RuleFilterEmpty,
                     TargetUnreachable)
from .filter import activate_account_vocab, filter_clauses
from .render import build_segments, build_virtual_segments, render_video
from .split import DEFAULT_MAX_DURATION, DEFAULT_MIN_DURATION, split_clauses
from .units import (DEFAULT_MERGE_MAX, DEFAULT_MERGE_MIN, DEFAULT_SILENCE_GAP,
                    build_units, order_candidates)

DEFAULT_TARGET = (70.0, 90.0)
# 超过这个长度的句单元不拆散：成片总时长允许相应超出目标上限，由人工再剪。
LONG_UNIT_SECONDS = 6.0
# S3 单次 AI 调用的子句上限：一次编排 400 条会超时，按批切分。
DEFAULT_JUDGE_BATCH = 120
# S3 批次并发度：默认 1 = 完全串行（保持既有行为），可用 PIPELINE_JUDGE_CONCURRENCY 覆盖。
DEFAULT_JUDGE_CONCURRENCY = 1
# S3 单批失败后的重试次数（整批重跑）：默认 1 = 首次失败再试 1 次；0 = 不重试。
# 模型偶发漏 id 是整段 S3 失败的主因，单批重跑成本低、能把这类抖动挡在任务外。
DEFAULT_JUDGE_RETRIES = 1

StageCallback = Callable[[str, str, str], None]


def _emit(on_stage: StageCallback | None, stage: str, status: str, message: str) -> None:
    if on_stage is not None:
        on_stage(stage, status, message)


def _duration(clause: dict[str, Any]) -> float:
    return float(clause["end"]) - float(clause["start"])


def _dump(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)


def _load_required(path: str, label: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        raise PipelineError(f"{label} 不存在，无法单独重跑当前节点：{path}")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"{label} 无法读取：{exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("clauses"), list):
        raise PipelineError(f"{label} 数据格式无效")
    return value


_INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe_output_stem(value: str | None) -> str:
    """把成片名称收敛成安全的文件名主体；空值回退 final。"""
    stem = _INVALID_FILENAME_RE.sub("-", str(value or "")).strip(" -.。")
    return stem[:80] or "final"


def next_available_output(folder: str, stem: str | None = None) -> str:
    """在 ``folder`` 下返回一个不覆盖已有文件的名字：首版 ``名字.mp4``，重名依次 ``名字-1.mp4``、
    ``名字-2.mp4``…（只递增数字，绝不覆盖）。
    """
    base = _safe_output_stem(stem)
    index = 0
    while True:
        name = f"{base}.mp4" if index == 0 else f"{base}-{index}.mp4"
        candidate = os.path.join(folder, name)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def next_available_dir(parent: str, name: str | None) -> str:
    """在 ``parent`` 下返回一个不覆盖已有目录的路径：首版 ``名字``，重名 ``名字-1``、``名字-2``…。"""
    base = _safe_output_stem(name)
    index = 0
    while True:
        candidate = os.path.join(parent, base if index == 0 else f"{base}-{index}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def _next_render_output(workdir: str, stem: str | None = None) -> str:
    """成片在工作目录 ``deliverables`` 下的输出路径；重名自动 +1，不覆盖历史成片。

    ``stem`` 为空时沿用 ``final``，保证既有任务与历史成片的命名不变。
    """
    return next_available_output(os.path.join(workdir, "deliverables"), stem)


def _default_asr(media: str, workdir: str, backend: str | None,
                 model: str | None) -> tuple[list[dict], list[dict], float]:
    sentences, words = asr_mod.transcribe(media, workdir, backend=backend, model=model)
    return sentences, words, asr_mod.probe_duration(media)


def _judge_batches(units: list[dict[str, Any]], max_clauses: int):
    """把句单元按子句数分批，保证单次 AI 调用的输入有界（不会一次编排 400 条）。"""
    batch: list[dict[str, Any]] = []
    count = 0
    for unit in units:
        size = len(unit["members"])
        if batch and count + size > max_clauses:
            yield batch
            batch, count = [], 0
        batch.append(unit)
        count += size
    if batch:
        yield batch


def _judge_concurrency(value: Any) -> int:
    """把并发配置收敛成正整数；非法/缺失回退默认串行（1），保证可回退。"""
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return DEFAULT_JUDGE_CONCURRENCY
    return resolved if resolved >= 1 else DEFAULT_JUDGE_CONCURRENCY


def _judge_retries(value: Any) -> int:
    """把重试配置收敛成非负整数；非法/缺失回退默认重试次数（0 表示不重试）。"""
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return DEFAULT_JUDGE_RETRIES
    return resolved if resolved >= 0 else DEFAULT_JUDGE_RETRIES


def _judge_batch_once(model: str, batch: list[dict[str, Any]],
                      product_name: str | None, timeout: int) -> dict[int, dict[str, Any]]:
    """一次独立的 S3 批次 AI 调用 + 契约校验，返回该批 ``{id: decision}``（单次尝试）。"""
    members = [clause for unit in batch for clause in unit["members"]]
    data = ai_call(model, _judge_prompt(batch, product_name), DECISION_SCHEMA, timeout)
    return _validate_decisions(data, members)


def _run_judge_batches(model: str, batches: list[list[dict[str, Any]]],
                       product_name: str | None, timeout: int,
                       concurrency: int = DEFAULT_JUDGE_CONCURRENCY,
                       retries: int = DEFAULT_JUDGE_RETRIES
                       ) -> tuple[dict[int, dict[str, Any]], int]:
    """执行 S3 各批次判定，返回 ``(按 id 合并的 decisions, AI 调用次数)``。

    每个批次始终是一次独立的无状态 AI 调用，结果只按子句 id 合并，故并发不改变识别
    结果。``concurrency <= 1`` 时完全串行（默认，保持既有行为）；大于 1 时用线程池并发
    提交，每批各自拉起一个独立子进程。单批返回不合契约（``AIReturnError``）时按
    ``retries`` 整批重跑（prompt 与串行完全一致，不改变识别结果）；仍失败则按批次顺序
    抛出第一个失败的异常，语义与串行一致。返回的调用次数含重试。
    """
    attempts = 0
    lock = Lock()

    def run_batch(batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        nonlocal attempts
        last: AIReturnError | None = None
        for _ in range(retries + 1):
            try:
                result = _judge_batch_once(model, batch, product_name, timeout)
                with lock:
                    attempts += 1
                return result
            except AIReturnError as exc:
                last = exc
                with lock:
                    attempts += 1
        raise last  # type: ignore[misc]

    decisions: dict[int, dict[str, Any]] = {}
    if concurrency <= 1 or len(batches) <= 1:
        for batch in batches:
            decisions.update(run_batch(batch))
        return decisions, attempts
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_batch, batch) for batch in batches]
        for future in futures:
            decisions.update(future.result())
    return decisions, attempts


def _judge_prompt(units: list[dict[str, Any]], product_name: str | None = None) -> str:
    payload = [{"unit": unit["id"],
                "parts": [{"id": clause["id"], "text": clause["text"]}
                          for clause in unit["members"]]}
               for unit in units]
    if product_name:
        headline = f"你在对一条女装直播口播的候选子句做可用性判定，本次主商品是「{product_name}」。\n"
        product_rule = (
            f"主商品判定以「{product_name}」为准：这句话主推、报价或报库存的对象不是"
            f"「{product_name}」时判 false；如果只是把「{product_name}」当作搭配、对比或"
            "上身参照来讲，则按主商品内容保留。\n")
    else:
        headline = "你在对一条女装直播口播的候选子句做可用性判定，主商品未知。\n"
        product_rule = ""
    return (
        "直接分析并输出结果，不要调用任何工具或执行命令行。\n"
        f"{headline}"
        "输入按「句单元」分组：同一单元的 parts 是直播里连续说下去、语义承接的一段话"
        "的逐子句切分，按顺序拼起来就是整句。请把每条子句放回它所在的整句单元里理解，"
        "再逐条判断。\n"
        "usable=true 需要同时满足：在这段语境里语义完整、可作为独立卖点进入成片；"
        "内容是在讲商品（款式/面料/版型/颜色/搭配/上身效果/口碑/痛点解决等长效卖点）。\n"
        "usable=false 的典型：直播场控/催拍、报库存/发货/物流、报价/促销福利、"
        "闲聊寒暄、非主商品、ASR 错乱读不通。\n"
        f"{product_rule}"
        "重要：不要因为一句单独看像残句就判 false——只要它和同单元相邻子句拼起来是"
        "一句完整的话，就判 true。\n"
        "硬性要求：必须恰好覆盖输入中的每个 id，不得新增、遗漏或重复；每个 id 都要"
        "给出 usable 布尔值与简短 reason。\n\n"
        f"输入句单元（JSON）：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _order_prompt(candidates: list[dict[str, Any]],
                  target: tuple[float, float],
                  product_name: str | None = None,
                  available: float | None = None) -> str:
    payload = [{"id": clause["id"], "text": clause["text"],
                "seconds": round(_duration(clause), 2)} for clause in candidates]
    low, high = target
    roles = "/".join(ORDER_SECTION_ROLES)
    product_line = (
        f"本片主商品固定为「{product_name}」，main_product 必须原样输出它；不要选择以其它"
        "商品为主推对象的句单元。\n" if product_name
        else "main_product 给出本片主商品（如「毛衣」）。\n")
    short_note = ""
    if available is not None and available < low:
        short_note = (
            f"注意：可用句单元总时长仅 {available:.2f} 秒，已低于目标下限 {low:g} 秒；"
            "请把可用句单元尽量全部选上，成片时长以实际可用为准。\n")
    return (
        "直接分析并输出结果，不要调用任何工具或执行命令行。\n"
        "你在把已判定可用的口播句单元，编成一条可直接发布到抖音的短视频，"
        "输出它的分段角色（sections）与播放顺序（ordered_ids）。\n"
        "成片结构（按此顺序输出；某角色没有合适素材就整段省略，不要硬凑）：\n"
        "1) hook 钩子：全片最前面，可由 1~多个句单元组成；整体要能独立听懂、有吸引力"
        "（反常识 / 具体数字 / 身份认同 / 痛点 / 最亮卖点），不必压到固定秒数。\n"
        "2) scene 场景痛点：0~2 段，说使用场景或用户痛点。\n"
        "3) selling_point 核心卖点：2~4 段，覆盖材质 / 版型 / 工艺 / 上身效果，"
        "每类最多 1 段，不得重复同一卖点。\n"
        "4) proof 信任对比：0~2 段，质量、对比、复购口碑等。\n"
        "5) styling 搭配颜色：0~2 段。\n"
        "6) cta 行动号召：0~1 段，只有存在合规、可执行的号召句时才选，且必须放在最后。\n"
        f"section 的 role 只能取 {roles}。\n"
        "硬性要求：\n"
        "- hook 必须是第一个 section；每个 section 至少 1 个 id；\n"
        "- ordered_ids = 各 section 的 ids 依次拼接，必须与 sections 完全一致"
        "（不新增、不遗漏、不重复）；\n"
        "- 所有 id 只能取自输入，不得越界；\n"
        f"- 总时长（所选 id 的 seconds 之和）落在 {low:g}~{high:g} 秒；单个超过 "
        f"{LONG_UNIT_SECONDS:g} 秒的句单元可正常选中，此时总时长允许相应超出上限"
        "（超出的部分交人工再剪）；\n"
        f"{short_note}"
        "- 全片围绕 main_product，面向目标人群；\n"
        "- 相邻段主题要承接，不得主题跳跃；同一卖点、同一颜色、同一价格类信息不得重复；\n"
        "- hook 不要选依赖前文的残句（如以「所以 / 然后 / 但是」开头且说不完整）；\n"
        "- 可以丢弃次要或重复的句单元。\n\n"
        f"{product_line}"
        f"可用句单元（按时间序，JSON）：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _validate_decisions(data: dict[str, Any],
                        candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise AIReturnError("S3 返回缺少 decisions 数组")
    expected = {clause["id"] for clause in candidates}
    seen: list[int] = []
    result: dict[int, dict[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise AIReturnError(f"S3 decision 不是对象：{decision!r}")
        cid = decision.get("id")
        if not isinstance(cid, int) or isinstance(cid, bool):
            raise AIReturnError(f"S3 decision id 非法：{cid!r}")
        if not isinstance(decision.get("usable"), bool):
            raise AIReturnError(f"S3 decision {cid} 的 usable 不是布尔值")
        if cid not in expected:
            # 模型常在子句 id 的空洞处“补号”，多余项不含信息，忽略即可；
            # 覆盖完整性由下面的 missing 检查兜底。
            continue
        seen.append(cid)
        result[cid] = decision
    if len(seen) != len(set(seen)):
        raise AIReturnError("S3 返回的 id 有重复")
    missing = sorted(expected - set(seen))
    if missing:
        raise AIReturnError(f"S3 id 覆盖不符：缺少 {missing}")
    return result


def _validate_order(data: dict[str, Any], candidates: list[dict[str, Any]],
                    target: tuple[float, float],
                    tolerance: float,
                    available: float | None = None
                    ) -> tuple[str, list[int], float, list[dict[str, Any]]]:
    """校验 S4 的结构化返回：sections（角色分段）与 ordered_ids 必须自洽。

    hook 必须开头，cta 若存在必须收尾；其余角色的先后不做过细限制，避免规则过死
    导致模型反复失败。合规/去噪不在这里判断（由 S2/S3 负责）。
    """
    main_product = data.get("main_product")
    if not isinstance(main_product, str) or not main_product.strip():
        raise AIReturnError("S4 返回缺少 main_product")
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise AIReturnError("S4 返回缺少非空 sections")
    by_id = {clause["id"]: clause for clause in candidates}
    sections: list[dict[str, Any]] = []
    flat: list[int] = []
    hook_count = 0
    for section in raw_sections:
        if not isinstance(section, dict):
            raise AIReturnError(f"S4 section 不是对象：{section!r}")
        role = section.get("role")
        if role not in ORDER_SECTION_ROLES:
            raise AIReturnError(f"S4 section role 非法：{role!r}")
        ids = section.get("ids")
        if not isinstance(ids, list) or not ids:
            raise AIReturnError(f"S4 section {role} 缺少非空 ids")
        section_ids: list[int] = []
        for cid in ids:
            if not isinstance(cid, int) or isinstance(cid, bool) or cid not in by_id:
                raise AIReturnError(f"S4 sections 含非法/越界 id：{cid!r}")
            section_ids.append(cid)
        if role == "hook":
            hook_count += 1
        sections.append({"role": role, "ids": section_ids})
        flat.extend(section_ids)
    if len(flat) != len(set(flat)):
        raise AIReturnError("S4 sections 的 id 有重复")
    if hook_count != 1 or sections[0]["role"] != "hook":
        raise AIReturnError("S4 必须恰好有一个 hook，且位于第一个 section")
    if any(section["role"] == "cta" for section in sections[:-1]):
        raise AIReturnError("S4 cta 只能作为最后一个 section")
    ordered_ids = data.get("ordered_ids")
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise AIReturnError("S4 返回缺少非空 ordered_ids")
    seen: list[int] = []
    for cid in ordered_ids:
        if not isinstance(cid, int) or isinstance(cid, bool) or cid not in by_id:
            raise AIReturnError(f"S4 ordered_ids 含非法/越界 id：{cid!r}")
        seen.append(cid)
    if len(seen) != len(set(seen)):
        raise AIReturnError("S4 ordered_ids 有重复")
    if seen != flat:
        raise AIReturnError("S4 ordered_ids 与 sections 的 ids 拼接不一致")
    total = sum(_duration(by_id[cid]) for cid in seen)
    low, high = target
    # 超过 6s 的句单元不拆散：它带来的超长允许顶高总时长上限，交由人工再剪。
    overflow = sum(max(0.0, _duration(by_id[cid]) - LONG_UNIT_SECONDS)
                   for cid in seen if _duration(by_id[cid]) > LONG_UNIT_SECONDS)
    # 可用素材本身就不够目标下限时，不允许因"时长不足"中断任务：把下限放宽到实际可用量。
    floor = low - tolerance
    if available is not None and available < floor:
        floor = available
    if total < floor or total > high + tolerance + overflow:
        raise AIReturnError(
            f"S4 排序总时长 {total:.2f}s 不在目标 {low:g}~{high:g}s 内"
            f"（长单元溢出容忍 {overflow:.2f}s）")
    return main_product.strip(), seen, total, sections


def run_pipeline_stage(media: str, workdir: str, stage: str, *,
                       target_seconds: tuple[float, float] = DEFAULT_TARGET,
                       target_tolerance: float = 1.0,
                       ai_model: str | None = None,
                       ai_timeout: int = DEFAULT_TIMEOUT,
                       min_duration: float = DEFAULT_MIN_DURATION,
                       merge_min: float | None = None,
                       merge_max: float | None = None,
                       judge_batch: int | None = None,
                       judge_concurrency: int | None = None,
                       judge_retries: int | None = None,
                       product_name: str | None = None,
                       output_stem: str | None = None,
                       render_size: tuple[int, int] | None = None,
                       preset: str | None = None,
                       select_visual_fn: Callable[[dict[str, Any]], tuple[float, float]]
                       | None = None,
                       on_stage: StageCallback | None = None) -> dict[str, Any]:
    """只重跑 S2/S3/S4/S6 中的一个节点，并严格复用其上游落盘数据。"""
    if stage not in {"filter", "judge", "order", "render"}:
        raise PipelineError(f"不支持单节点重跑：{stage}")
    media = os.path.abspath(media)
    workdir = os.path.abspath(workdir)
    # 单节点重跑 S2 必须与全量运行用同一份账号红线词表，否则会出现「全量已删、
    # 重跑又放行」的矛盾。
    activate_account_vocab()
    model = ai_model or os.environ.get("PIPELINE_AI_MODEL") or "auto"
    if merge_min is None:
        merge_min = float(os.environ.get("PIPELINE_MERGE_MIN") or DEFAULT_MERGE_MIN)
    if merge_max is None:
        merge_max = float(os.environ.get("PIPELINE_MERGE_MAX") or DEFAULT_MERGE_MAX)
    if judge_batch is None:
        judge_batch = int(os.environ.get("PIPELINE_JUDGE_BATCH") or DEFAULT_JUDGE_BATCH)
    if judge_concurrency is None:
        judge_concurrency = _judge_concurrency(os.environ.get("PIPELINE_JUDGE_CONCURRENCY"))
    if judge_retries is None:
        judge_retries = _judge_retries(os.environ.get("PIPELINE_JUDGE_RETRIES"))

    if stage == "filter":
        base = _load_required(os.path.join(workdir, "clauses.json"), "S1 子句数据")
        clauses = copy.deepcopy(base["clauses"])
        _emit(on_stage, "filter", "start", "单独重跑规则粗筛（复用 S1 数据）")
        filter_clauses(clauses, min_duration=min_duration)
        usable = [clause for clause in clauses if clause["usable"]]
        if not usable:
            raise RuleFilterEmpty(f"S2 规则筛后无可用子句（共 {len(clauses)} 条全部被剔除）")
        result = {"source": media, "duration": float(base.get("duration") or 0.0),
                  "clauses": clauses}
        _dump(os.path.join(workdir, "clauses.filtered.json"), result)
        # timeline 回到纯 S1 状态，确保旧 S3/S4 字段不再被误当成当前结果。
        _dump(os.path.join(workdir, "timeline.json"), {
            "source": media, "duration": result["duration"],
            "clauses": copy.deepcopy(base["clauses"]),
        })
        _emit(on_stage, "filter", "done",
              f"规则筛后剩 {len(usable)}/{len(clauses)} 条可用子句；S3/S4 已清空")
        return {"stage": stage, "clauses": len(clauses), "usable": len(usable)}

    if stage == "judge":
        filtered = _load_required(os.path.join(workdir, "clauses.filtered.json"),
                                  "S2 规则粗筛数据")
        clauses = copy.deepcopy(filtered["clauses"])
        usable = [clause for clause in clauses if clause.get("usable")]
        if not usable:
            raise RuleFilterEmpty("S2 数据中没有可供 AI 判定的子句")
        units = build_units(usable, merge_min=merge_min, merge_max=merge_max,
                            silence_gap=DEFAULT_SILENCE_GAP)
        batches = list(_judge_batches(units, judge_batch))
        concurrency_note = f"，并发 {judge_concurrency}" if judge_concurrency > 1 else ""
        _emit(on_stage, "judge", "start",
              f"单独重跑 AI 可用性判定（{len(usable)} 条 / {len(batches)} 批{concurrency_note}）")
        decisions, _ = _run_judge_batches(model, batches, product_name, ai_timeout,
                                          judge_concurrency, judge_retries)
        for clause in usable:
            decision = decisions[clause["id"]]
            clause["usable"] = bool(decision["usable"])
            clause["reason"] = str(decision.get("reason") or "")
            clause.pop("order", None)
        timeline = {"source": media, "duration": float(filtered.get("duration") or 0.0),
                    "clauses": clauses}
        _dump(os.path.join(workdir, "timeline.json"), timeline)
        _dump(os.path.join(workdir, "clauses.judged.json"),
              {"source": media, "duration": timeline["duration"], "clauses": usable})
        judged = [clause for clause in clauses if clause.get("usable")]
        if not judged:
            raise AIReturnError("S3 判定后没有任何可用子句")
        candidates = order_candidates(usable)
        _emit(on_stage, "judge", "done",
              f"AI 判定后剩 {len(judged)} 条可用子句；S4 已清空")
        return {"stage": stage, "usable": len(judged), "candidates": len(candidates)}

    if stage == "render":
        judged_data = _load_required(os.path.join(workdir, "timeline.json"), "S3 AI 判定数据")
        clauses = copy.deepcopy(judged_data["clauses"])
        usable = [clause for clause in clauses if clause.get("usable")]
        candidates = order_candidates(usable)
        order_path = os.path.join(workdir, "order.json")
        if not os.path.isfile(order_path):
            raise PipelineError("S4 排序结果不存在，请先执行 S4 排序编排")
        try:
            with open(order_path, encoding="utf-8") as handle:
                order_data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise PipelineError(f"S4 排序结果无法读取：{exc}") from exc
        position = {cid: index for index, cid
                    in enumerate(order_data.get("ordered_ids") or [])}
        by_id = {candidate["id"]: candidate for candidate in candidates}
        ordered_clauses = sorted((by_id[cid] for cid in position if cid in by_id),
                                 key=lambda candidate: position[candidate["id"]])
        if not ordered_clauses:
            raise PipelineError("S4 排序结果为空，无法渲染成片")
        _emit(on_stage, "render", "start", "单独重跑渲染成片（复用 S4 排序结果）")
        segments = build_segments(ordered_clauses, select_visual_fn)
        output = _next_render_output(workdir, output_stem)
        render_video(media, segments, output, workdir,
                     width=render_size[0] if render_size else None,
                     height=render_size[1] if render_size else None,
                     preset=preset)
        manifest = {
            "source": media,
            "duration": float(judged_data.get("duration") or 0.0),
            "main_product": order_data.get("main_product") or "",
            "order_sections": order_data.get("sections") or [],
            "ai_calls": 0,
            "ai_engine": os.environ.get("PIPELINE_AI_ENGINE") or "llm",
            "ai_provider": os.environ.get("PIPELINE_AI_PROVIDER") or "auto",
            "ai_model": model,
            "target_seconds": {"min": target_seconds[0], "max": target_seconds[1]},
            "total_seconds": round(float(order_data.get("total_seconds") or 0.0), 3),
            "clauses": len(clauses),
            "usable_clauses": len(usable),
            "order_candidates": len(candidates),
            "judge_batch": judge_batch,
            "judge_concurrency": judge_concurrency,
            "judge_retries": judge_retries,
            "segments": segments,
            "output": os.path.relpath(output, workdir),
        }
        _dump(os.path.join(workdir, "manifest.json"), manifest)
        _emit(on_stage, "render", "done",
              f"成片已生成：{manifest['output']}（{manifest['total_seconds']}s）")
        return manifest

    judged_data = _load_required(os.path.join(workdir, "timeline.json"), "S3 AI 判定数据")
    clauses = copy.deepcopy(judged_data["clauses"])
    usable = [clause for clause in clauses if clause.get("usable")]
    candidates = order_candidates(usable)
    available = sum(_duration(candidate) for candidate in candidates)
    if available < target_seconds[0] - target_tolerance:
        _emit(on_stage, "order", "start",
              f"单独重跑 AI 排序编排：可用仅 {available:.2f}s，低于目标下限 "
              f"{target_seconds[0]:g}s，按实际时长出片")
    else:
        _emit(on_stage, "order", "start",
              f"单独重跑 AI 排序编排（目标 {target_seconds[0]:g}~{target_seconds[1]:g}s）")
    order = ai_call(model, _order_prompt(candidates, target_seconds, product_name, available),
                    ORDER_SCHEMA, ai_timeout)
    main_product, ordered_ids, total_seconds, sections = _validate_order(
        order, candidates, target_seconds, target_tolerance, available)
    main_product = product_name or main_product
    position = {cid: index for index, cid in enumerate(ordered_ids)}
    member_candidate = {member: candidate["id"]
                        for candidate in candidates for member in candidate["members"]}
    for clause in clauses:
        clause["order"] = position.get(member_candidate.get(clause["id"]))
    timeline = {"source": media, "duration": float(judged_data.get("duration") or 0.0),
                "clauses": clauses}
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    result = {"stage": stage, "main_product": main_product, "sections": sections,
              "ordered_ids": ordered_ids, "total_seconds": round(total_seconds, 3)}
    _dump(os.path.join(workdir, "order.json"), result)
    _emit(on_stage, "order", "done",
          f"选出 {len(ordered_ids)} 段、共 {total_seconds:.2f}s；上游数据保持不变")
    return result


def run_pipeline(media: str, workdir: str, *,
                 target_seconds: tuple[float, float] = DEFAULT_TARGET,
                 target_tolerance: float = 1.0,
                 ai_model: str | None = None,
                 ai_timeout: int = DEFAULT_TIMEOUT,
                 max_duration: float = DEFAULT_MAX_DURATION,
                 min_duration: float = DEFAULT_MIN_DURATION,
                 merge_min: float | None = None,
                 merge_max: float | None = None,
                 judge_batch: int | None = None,
                 judge_concurrency: int | None = None,
                 judge_retries: int | None = None,
                 asr_backend: str | None = None,
                 asr_model: str | None = None,
                 transcript: tuple[list[dict], list[dict]] | None = None,
                 asr_fn: Callable[..., tuple[list[dict], list[dict], float]] | None = None,
                 select_visual_fn: Callable[[dict[str, Any]], tuple[float, float]]
                 | None = None,
                 render_size: tuple[int, int] | None = None,
                 preset: str | None = None,
                 output_stem: str | None = None,
                 product_name: str | None = None,
                 on_stage: StageCallback | None = None,
                 virtual_timeline: Any | None = None) -> dict[str, Any]:
    """跑完 S1→S2→S3→S4→S6，返回结果摘要并落盘 timeline/manifest 与成片。

    ``output_stem`` 是成片文件名主体（来自任务标题）；为空时沿用 ``final``，
    重名时自动加序号为 ``<stem>-1.mp4``。``product_name`` 是本次要剪辑的主商品，
    非空时贯穿 S3/S4 用于剔除主推其它商品的句子。``on_stage(stage_id, status,
    message)`` 在每一步开始/结束时回调，供上层（如 Web 任务详情）展示进度；
    ``status`` 为 ``"start"`` 或 ``"done"``。
    """
    media = os.path.abspath(media)
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    # S2 必须应用账号级红线词表（价格/现货/发货/库存/尺码/优惠券等），否则大量
    # 红线子句会漏进 S3，白白增加批次与耗时。
    activate_account_vocab()
    model = ai_model or os.environ.get("PIPELINE_AI_MODEL") or "auto"
    if merge_min is None:
        merge_min = float(os.environ.get("PIPELINE_MERGE_MIN") or DEFAULT_MERGE_MIN)
    if merge_max is None:
        merge_max = float(os.environ.get("PIPELINE_MERGE_MAX") or DEFAULT_MERGE_MAX)
    if judge_batch is None:
        judge_batch = int(os.environ.get("PIPELINE_JUDGE_BATCH") or DEFAULT_JUDGE_BATCH)
    if judge_concurrency is None:
        judge_concurrency = _judge_concurrency(os.environ.get("PIPELINE_JUDGE_CONCURRENCY"))
    if judge_retries is None:
        judge_retries = _judge_retries(os.environ.get("PIPELINE_JUDGE_RETRIES"))

    # S1 —— ASR + 子句切分（确定性）
    timeline_path = os.path.join(workdir, "timeline.json")
    clauses_path = os.path.join(workdir, "clauses.json")
    source_name = virtual_timeline.title if virtual_timeline is not None else media
    cached_timeline: dict[str, Any] | None = None
    if transcript is None and asr_fn is None:
        for candidate_path in (timeline_path, clauses_path):
            if os.path.isfile(candidate_path):
                try:
                    with open(candidate_path, encoding="utf-8") as handle:
                        data = json.load(handle)
                    if (isinstance(data, dict)
                            and os.path.normcase(os.path.normpath(str(data.get("source", ""))))
                            == os.path.normcase(os.path.normpath(source_name))
                            and isinstance(data.get("clauses"), list)
                            and len(data["clauses"]) > 0):
                        cached_timeline = data
                        break
                except Exception:
                    pass

    words: list[dict] | None = None
    if cached_timeline is not None:
        _emit(on_stage, "asr", "start", "复用已有语音转写与子句切分")
        clauses = cached_timeline["clauses"]
        duration = float(cached_timeline.get("duration") or 0.0)
        timeline = cached_timeline
        _dump(timeline_path, timeline)
        _dump(clauses_path, timeline)
        _emit(on_stage, "asr", "done", f"复用已有 {len(clauses)} 个子句")
    else:
        _emit(on_stage, "asr", "start", "语音转写与子句切分")
        if virtual_timeline is not None:
            if transcript is not None:
                sentences, words = transcript
                duration = virtual_timeline.total_duration
            elif asr_fn is not None:
                sentences, words, duration = asr_fn(media, workdir)
            else:
                from ..timeline import extract_virtual_timeline_audio
                virtual_wav = extract_virtual_timeline_audio(
                    virtual_timeline, os.path.join(workdir, "virtual_audio.wav"), workdir)
                sentences, words = asr_mod.transcribe(virtual_wav, workdir,
                                                      backend=asr_backend, model=asr_model)
                duration = virtual_timeline.total_duration
        else:
            if transcript is not None:
                sentences, words = transcript
                duration = max([float(word["e"]) for word in words] + [0.0])
            elif asr_fn is not None:
                sentences, words, duration = asr_fn(media, workdir)
            else:
                sentences, words, duration = _default_asr(media, workdir, asr_backend, asr_model)

        clauses = split_clauses(words, sentences, max_duration=max_duration,
                                min_duration=min_duration)
        if not clauses:
            raise AsrError("S1 没有产出任何子句（无标记过的词级时间戳）")

        if virtual_timeline is not None:
            for clause in clauses:
                virtual_timeline.map_clause(clause)
            timeline = {
                "source": source_name,
                "duration": round(float(duration), 3),
                "timeline_id": virtual_timeline.timeline_id,
                "clauses": clauses,
                "virtual_timeline": virtual_timeline.to_dict(),
            }
        else:
            timeline = {"source": media, "duration": round(float(duration), 3),
                        "clauses": clauses}

        _dump(timeline_path, timeline)
        _dump(clauses_path, timeline)
        _emit(on_stage, "asr", "done", f"切出 {len(clauses)} 个子句")

    # S2 —— 规则粗筛（确定性）
    _emit(on_stage, "filter", "start", "规则粗筛（违禁/价格/场控/去重）")
    filter_clauses(clauses, min_duration=min_duration)
    _dump(os.path.join(workdir, "clauses.filtered.json"),
          {"source": source_name, "duration": timeline["duration"], "clauses": clauses})
    usable = [clause for clause in clauses if clause["usable"]]
    if not usable:
        raise RuleFilterEmpty(f"S2 规则筛后无可用子句（共 {len(clauses)} 条全部被剔除）")
    _emit(on_stage, "filter", "done",
          f"规则筛后剩 {len(usable)}/{len(clauses)} 条可用子句")

    ai_calls = 0

    # S2.5 —— 句单元聚合（确定性）：给 S3 提供上下文，逐子句结论不变
    units = build_units(usable, merge_min=merge_min, merge_max=merge_max,
                        silence_gap=DEFAULT_SILENCE_GAP)

    # S3 —— AI 判定：按批切分，单次调用有界，避免一次编排 400 条超时
    batches = list(_judge_batches(units, judge_batch))
    concurrency_note = f" / 并发 {judge_concurrency}" if judge_concurrency > 1 else ""
    _emit(on_stage, "judge", "start",
          f"AI 可用性判定（{len(usable)} 条子句 / {len(units)} 个句单元 / "
          f"{len(batches)} 批{concurrency_note}）")
    decisions, judge_calls = _run_judge_batches(model, batches, product_name, ai_timeout,
                                                judge_concurrency, judge_retries)
    ai_calls += judge_calls
    by_id = decisions
    for clause in usable:
        decision = by_id[clause["id"]]
        clause["usable"] = bool(decision["usable"])
        clause["reason"] = str(decision.get("reason") or "")
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "clauses.judged.json"),
          {"source": source_name, "duration": timeline["duration"], "clauses": usable})
    judged = [clause for clause in clauses if clause["usable"]]
    if not judged:
        raise AIReturnError("S3 判定后没有任何可用子句")
    # S4 候选 = 同一句单元内连续的可用子句并为一段（保证半句话不会单独入片）
    candidates = order_candidates(usable)
    available = sum(_duration(candidate) for candidate in candidates)
    _emit(on_stage, "judge", "done",
          f"AI 判定后剩 {len(judged)} 条可用子句 / {len(candidates)} 段可用句单元")
    if available < target_seconds[0] - target_tolerance:
        _emit(on_stage, "order", "start",
              f"可用句单元总时长仅 {available:.2f}s，低于目标下限 "
              f"{target_seconds[0]:g}s，按实际时长出片（不中断）")
    else:
        _emit(on_stage, "order",
              "start", f"AI 排序编排（目标 {target_seconds[0]:g}~{target_seconds[1]:g}s）")
    order = ai_call(model, _order_prompt(candidates, target_seconds, product_name, available),
                    ORDER_SCHEMA, ai_timeout)
    ai_calls += 1
    main_product, ordered_ids, total_seconds, sections = _validate_order(
        order, candidates, target_seconds, target_tolerance, available)
    main_product = product_name or main_product
    position = {cid: index for index, cid in enumerate(ordered_ids)}
    member_candidate = {member: candidate["id"]
                        for candidate in candidates for member in candidate["members"]}
    for clause in clauses:
        clause["order"] = position.get(member_candidate.get(clause["id"]))
    ordered_clauses = sorted((candidate for candidate in candidates
                              if candidate["id"] in position),
                             key=lambda candidate: position[candidate["id"]])
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "order.json"), {
        "stage": "order", "main_product": main_product, "sections": sections,
        "ordered_ids": ordered_ids, "total_seconds": round(total_seconds, 3),
    })
    _emit(on_stage, "order", "done",
          f"选出 {len(ordered_clauses)} 段、共 {total_seconds:.2f}s")

    # S6 —— 渲染（确定性）
    _emit(on_stage, "render", "start", "ffmpeg 逐段剪切并拼接")
    output = _next_render_output(workdir, output_stem)
    if virtual_timeline is not None:
        segments = build_virtual_segments(ordered_clauses, virtual_timeline)
        render_video(media, segments, output, workdir,
                     width=render_size[0] if render_size else None,
                     height=render_size[1] if render_size else None,
                     preset=preset, virtual_timeline=virtual_timeline)
    else:
        segments = build_segments(ordered_clauses, select_visual_fn)
        render_video(media, segments, output, workdir,
                     width=render_size[0] if render_size else None,
                     height=render_size[1] if render_size else None,
                     preset=preset)

    manifest = {
        "source": source_name,
        "timeline_id": virtual_timeline.timeline_id if virtual_timeline else None,
        "virtual_timeline": virtual_timeline.to_dict() if virtual_timeline else None,
        "duration": timeline["duration"],
        "main_product": main_product,
        "order_sections": sections,
        "ai_calls": ai_calls,
        "ai_engine": os.environ.get("PIPELINE_AI_ENGINE") or "llm",
        "ai_provider": os.environ.get("PIPELINE_AI_PROVIDER") or "auto",
        "ai_model": model,
        "target_seconds": {"min": target_seconds[0], "max": target_seconds[1]},
        "total_seconds": round(total_seconds, 3),
        "clauses": len(clauses),
        "usable_clauses": len(judged),
        "sentence_units": len(units),
        "order_candidates": len(candidates),
        "merge_seconds": {"min": merge_min, "max": merge_max},
        "judge_batch": judge_batch,
        "judge_concurrency": judge_concurrency,
        "judge_retries": judge_retries,
        "segments": segments,
        "output": os.path.relpath(output, workdir),
        "transcript_words": len(words) if words is not None else sum(len(c.get("text", "")) for c in clauses),
    }
    _dump(os.path.join(workdir, "manifest.json"), manifest)
    _emit(on_stage, "render", "done",
          f"成片已生成：{manifest['output']}（{manifest['total_seconds']}s）")
    return manifest


def _mock_ai(model: str, prompt: str, schema: dict[str, Any],
             timeout: int) -> dict[str, Any]:
    """仅供本地/测试的确定性替身：判定全可用，按时间序贪心凑够目标时长。"""
    import re
    ids = [int(value) for value in re.findall(r'"id":\s*(\d+)', prompt)]
    if "decisions" in schema.get("properties", {}):
        return {"decisions": [{"id": cid, "usable": True, "reason": "mock"}
                              for cid in ids]}
    seconds = {int(cid): float(value) for cid, value in
               re.findall(r'"id":\s*(\d+),\s*"text":\s*"[^"]*",\s*"seconds":\s*([\d.]+)', prompt)}
    match = re.search(r"落在\s*([\d.]+)~([\d.]+)\s*秒", prompt)
    low, high = (float(match.group(1)), float(match.group(2))) if match else (0.0, 1e9)
    chosen: list[int] = []
    total = 0.0
    for cid in ids:
        value = seconds.get(cid, 0.0)
        if total + value <= high:
            chosen.append(cid)
            total += value
        if total >= low:
            break
    return {"main_product": "主商品",
            "sections": [{"role": "hook", "ids": chosen}],
            "ordered_ids": chosen}


def _parse_target(value: str) -> tuple[float, float]:
    low, _, high = value.partition("-")
    try:
        return float(low), float(high or low)
    except ValueError:
        raise argparse.ArgumentTypeError(f"目标时长格式应为 MIN-MAX，收到 {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="精简切片管线（ASR→筛→2次AI→切→渲染）")
    parser.add_argument("--media", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--target", type=_parse_target, default=DEFAULT_TARGET,
                        help="目标总时长，形如 70-90")
    parser.add_argument("--target-tolerance", type=float, default=1.0)
    parser.add_argument("--ai-model", default=None)
    parser.add_argument("--ai-engine", default=None, choices=["llm", "jev"])
    parser.add_argument("--ai-provider", default=None)
    parser.add_argument("--ai-timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--backend", default=None, help="ASR 后端 auto/mlx/faster")
    parser.add_argument("--model", default=None, help="ASR 模型")
    parser.add_argument("--min-clause", type=float, default=DEFAULT_MIN_DURATION)
    parser.add_argument("--max-clause", type=float, default=DEFAULT_MAX_DURATION)
    parser.add_argument("--merge-min", type=float, default=None,
                        help="句单元聚合下限秒数（默认 4，可用 PIPELINE_MERGE_MIN 覆盖）")
    parser.add_argument("--merge-max", type=float, default=None,
                        help="句单元聚合上限秒数（默认 8，可用 PIPELINE_MERGE_MAX 覆盖）")
    parser.add_argument("--judge-batch", type=int, default=None,
                        help="S3 单次 AI 调用的子句上限（默认 120，可用 PIPELINE_JUDGE_BATCH 覆盖）")
    parser.add_argument("--judge-concurrency", type=int, default=None,
                        help="S3 批次并发度（默认 1=串行，可用 PIPELINE_JUDGE_CONCURRENCY 覆盖）")
    parser.add_argument("--judge-retries", type=int, default=None,
                        help="S3 单批失败后的重试次数（默认 1，可用 PIPELINE_JUDGE_RETRIES 覆盖）")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--preset")
    parser.add_argument("--timeline", help="剪映草稿目录/draft_content.json 或虚拟时间线 JSON 文件")
    parser.add_argument("--words-json", help="复用已有词级时间戳，跳过 ASR")
    parser.add_argument("--sentences-json", help="复用已有句级分段（仅用于 split_from）")
    parser.add_argument("--mock-ai", action="store_true",
                        help="用确定性替身替代 2 次 AI 调用（仅用于本地/CI 打通链路）")
    args = parser.parse_args(argv)

    if args.mock_ai:
        global ai_call
        ai_call = _mock_ai

    if args.ai_engine:
        os.environ["PIPELINE_AI_ENGINE"] = args.ai_engine
    if args.ai_provider:
        os.environ["PIPELINE_AI_PROVIDER"] = args.ai_provider

    virtual_timeline = None
    if args.timeline:
        from ..timeline import load_virtual_timeline
        virtual_timeline = load_virtual_timeline(args.timeline)

    transcript = None
    if args.words_json or args.sentences_json:
        transcript = asr_mod.load_transcript(args.words_json, args.sentences_json)

    try:
        manifest = run_pipeline(
            args.media, args.workdir, target_seconds=args.target,
            target_tolerance=args.target_tolerance, ai_model=args.ai_model,
            ai_timeout=args.ai_timeout, max_duration=args.max_clause,
            min_duration=args.min_clause, merge_min=args.merge_min,
            merge_max=args.merge_max, judge_batch=args.judge_batch,
            judge_concurrency=args.judge_concurrency,
            judge_retries=args.judge_retries,
            asr_backend=args.backend,
            asr_model=args.model, transcript=transcript,
            render_size=(args.width, args.height) if args.width and args.height else None,
            preset=args.preset, virtual_timeline=virtual_timeline)
    except PipelineError as exc:
        print(f"[{exc.stage}] {exc}", file=sys.stderr)
        return 2
    print(f"成片：{os.path.join(args.workdir, manifest['output'])}")
    print(f"主商品：{manifest['main_product']}  总时长：{manifest['total_seconds']}s  "
          f"AI 调用：{manifest['ai_calls']} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
