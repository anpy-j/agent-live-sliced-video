# -*- coding: utf-8 -*-
"""精简管线编排 + 独立入口。

一条命令：ASR → 规则筛 → 2 次无状态 AI 判定/排序 → 按时间戳切 → ffmpeg 拼接。

    python -m agent_video.pipeline.run --media /abs/素材.mp4 --workdir /abs/out

唯一数据契约是 ``timeline.json``：子句数组，全程只往对象上写字段。失败按
``errors.PipelineError`` 的子类区分（规则筛空 / AI 报错 / 渲染失败），不做兜底。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from . import asr as asr_mod
from .ai import DEFAULT_TIMEOUT, DECISION_SCHEMA, ORDER_SCHEMA, ai_call
from .errors import (AIReturnError, AsrError, PipelineError, RuleFilterEmpty,
                     TargetUnreachable)
from .filter import filter_clauses
from .render import build_segments, render_video
from .split import DEFAULT_MAX_DURATION, DEFAULT_MIN_DURATION, split_clauses
from .units import (DEFAULT_MERGE_MAX, DEFAULT_MERGE_MIN, DEFAULT_SILENCE_GAP,
                    build_units, order_candidates)

DEFAULT_TARGET = (45.0, 60.0)
# 超过这个长度的句单元不拆散：成片总时长允许相应超出目标上限，由人工再剪。
LONG_UNIT_SECONDS = 6.0
# S3 单次 AI 调用的子句上限：一次编排 400 条会超时，按批切分。
DEFAULT_JUDGE_BATCH = 120

StageCallback = Callable[[str, str, str], None]


def _emit(on_stage: StageCallback | None, stage: str, status: str, message: str) -> None:
    if on_stage is not None:
        on_stage(stage, status, message)


def _duration(clause: dict[str, Any]) -> float:
    return float(clause["end"]) - float(clause["start"])


def _dump(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)


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


def _judge_prompt(units: list[dict[str, Any]]) -> str:
    payload = [{"unit": unit["id"],
                "parts": [{"id": clause["id"], "text": clause["text"]}
                          for clause in unit["members"]]}
               for unit in units]
    return (
        "你在对一条女装直播口播的候选子句做可用性判定，主商品未知。\n"
        "输入按「句单元」分组：同一单元的 parts 是直播里连续说下去、语义承接的一段话"
        "的逐子句切分，按顺序拼起来就是整句。请把每条子句放回它所在的整句单元里理解，"
        "再逐条判断。\n"
        "usable=true 需要同时满足：在这段语境里语义完整、可作为独立卖点进入成片；"
        "内容是在讲商品（款式/面料/版型/颜色/搭配/上身效果/口碑/痛点解决等长效卖点）。\n"
        "usable=false 的典型：直播场控/催拍、报库存/发货/物流、报价/促销福利、"
        "闲聊寒暄、非主商品、ASR 错乱读不通。\n"
        "重要：不要因为一句单独看像残句就判 false——只要它和同单元相邻子句拼起来是"
        "一句完整的话，就判 true。\n"
        "硬性要求：必须恰好覆盖输入中的每个 id，不得新增、遗漏或重复；每个 id 都要"
        "给出 usable 布尔值与简短 reason。\n\n"
        f"输入句单元（JSON）：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _order_prompt(candidates: list[dict[str, Any]],
                  target: tuple[float, float]) -> str:
    payload = [{"id": clause["id"], "text": clause["text"],
                "seconds": round(_duration(clause), 2)} for clause in candidates]
    low, high = target
    return (
        "你在把已判定可用的口播句单元排成一条短视频成片的播放顺序。\n"
        f"硬性要求：ordered_ids 只能取自输入中的 id；总时长（所选 id 的 seconds 之和）"
        f"应落在 {low:g}~{high:g} 秒之间；开头放最有吸引力的一句；同一卖点不重复；"
        "可以丢弃次要或重复的句单元。\n"
        f"如果一个句单元本身超过 {LONG_UNIT_SECONDS:g} 秒，可以正常选它；此时成片总时长"
        "允许相应超过上限（超出的部分会由人工再剪）。\n"
        "main_product 给出本片主商品（如「马甲」）。\n\n"
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
                    tolerance: float) -> tuple[str, list[int], float]:
    main_product = data.get("main_product")
    if not isinstance(main_product, str) or not main_product.strip():
        raise AIReturnError("S4 返回缺少 main_product")
    ordered_ids = data.get("ordered_ids")
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise AIReturnError("S4 返回缺少非空 ordered_ids")
    by_id = {clause["id"]: clause for clause in candidates}
    seen: list[int] = []
    for cid in ordered_ids:
        if not isinstance(cid, int) or isinstance(cid, bool) or cid not in by_id:
            raise AIReturnError(f"S4 ordered_ids 含非法/越界 id：{cid!r}")
        seen.append(cid)
    if len(seen) != len(set(seen)):
        raise AIReturnError("S4 ordered_ids 有重复")
    total = sum(_duration(by_id[cid]) for cid in seen)
    low, high = target
    # 超过 6s 的句单元不拆散：它带来的超长允许顶高总时长上限，交由人工再剪。
    overflow = sum(max(0.0, _duration(by_id[cid]) - LONG_UNIT_SECONDS)
                   for cid in seen if _duration(by_id[cid]) > LONG_UNIT_SECONDS)
    if total < low - tolerance or total > high + tolerance + overflow:
        raise AIReturnError(
            f"S4 排序总时长 {total:.2f}s 不在目标 {low:g}~{high:g}s 内"
            f"（长单元溢出容忍 {overflow:.2f}s）")
    return main_product.strip(), seen, total


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
                 asr_backend: str | None = None,
                 asr_model: str | None = None,
                 transcript: tuple[list[dict], list[dict]] | None = None,
                 asr_fn: Callable[..., tuple[list[dict], list[dict], float]] | None = None,
                 select_visual_fn: Callable[[dict[str, Any]], tuple[float, float]]
                 | None = None,
                 render_size: tuple[int, int] | None = None,
                 preset: str | None = None,
                 on_stage: StageCallback | None = None) -> dict[str, Any]:
    """跑完 S1→S2→S3→S4→S6，返回结果摘要并落盘 timeline/manifest/final.mp4。

    ``on_stage(stage_id, status, message)`` 在每一步开始/结束时回调，供上层
    （如 Web 任务详情）展示进度；``status`` 为 ``"start"`` 或 ``"done"``。
    """
    media = os.path.abspath(media)
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    model = ai_model or os.environ.get("PIPELINE_AI_MODEL") or "auto"
    if merge_min is None:
        merge_min = float(os.environ.get("PIPELINE_MERGE_MIN") or DEFAULT_MERGE_MIN)
    if merge_max is None:
        merge_max = float(os.environ.get("PIPELINE_MERGE_MAX") or DEFAULT_MERGE_MAX)
    if judge_batch is None:
        judge_batch = int(os.environ.get("PIPELINE_JUDGE_BATCH") or DEFAULT_JUDGE_BATCH)

    # S1 —— ASR + 子句切分（确定性）
    _emit(on_stage, "asr", "start", "语音转写与子句切分")
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
    timeline: dict[str, Any] = {"source": media, "duration": round(float(duration), 3),
                                "clauses": clauses}
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "clauses.json"), timeline)
    _emit(on_stage, "asr", "done", f"切出 {len(clauses)} 个子句")

    # S2 —— 规则粗筛（确定性）
    _emit(on_stage, "filter", "start", "规则粗筛（违禁/价格/场控/去重）")
    filter_clauses(clauses, min_duration=min_duration)
    _dump(os.path.join(workdir, "clauses.filtered.json"),
          {"source": media, "duration": timeline["duration"], "clauses": clauses})
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
    _emit(on_stage, "judge", "start",
          f"AI 可用性判定（{len(usable)} 条子句 / {len(units)} 个句单元 / "
          f"{len(batches)} 批）")
    decisions: dict[int, dict[str, Any]] = {}
    for batch in batches:
        members = [clause for unit in batch for clause in unit["members"]]
        data = ai_call(model, _judge_prompt(batch), DECISION_SCHEMA, ai_timeout)
        ai_calls += 1
        decisions.update(_validate_decisions(data, members))
    by_id = decisions
    for clause in usable:
        decision = by_id[clause["id"]]
        clause["usable"] = bool(decision["usable"])
        clause["reason"] = str(decision.get("reason") or "")
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "clauses.judged.json"),
          {"source": media, "duration": timeline["duration"], "clauses": usable})
    judged = [clause for clause in clauses if clause["usable"]]
    if not judged:
        raise AIReturnError("S3 判定后没有任何可用子句")
    # S4 候选 = 同一句单元内连续的可用子句并为一段（保证半句话不会单独入片）
    candidates = order_candidates(usable)
    judged_seconds = sum(_duration(candidate) for candidate in candidates)
    if judged_seconds < target_seconds[0] - target_tolerance:
        raise TargetUnreachable(
            f"S3 判定后可用句单元总时长仅 {judged_seconds:.2f}s，低于目标下限 "
            f"{target_seconds[0]:g}s，S4 无法排出达标成片")
    _emit(on_stage, "judge", "done",
          f"AI 判定后剩 {len(judged)} 条可用子句 / {len(candidates)} 段可用句单元")

    # S4 —— AI 排序（仅 1 次调用）
    _emit(on_stage, "order",
          "start", f"AI 排序编排（目标 {target_seconds[0]:g}~{target_seconds[1]:g}s）")
    order = ai_call(model, _order_prompt(candidates, target_seconds),
                    ORDER_SCHEMA, ai_timeout)
    ai_calls += 1
    main_product, ordered_ids, total_seconds = _validate_order(
        order, candidates, target_seconds, target_tolerance)
    position = {cid: index for index, cid in enumerate(ordered_ids)}
    member_candidate = {member: candidate["id"]
                        for candidate in candidates for member in candidate["members"]}
    for clause in clauses:
        clause["order"] = position.get(member_candidate.get(clause["id"]))
    ordered_clauses = sorted((candidate for candidate in candidates
                              if candidate["id"] in position),
                             key=lambda candidate: position[candidate["id"]])
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _emit(on_stage, "order", "done",
          f"选出 {len(ordered_clauses)} 段、共 {total_seconds:.2f}s")

    # S6 —— 渲染（确定性）
    _emit(on_stage, "render", "start", "ffmpeg 逐段剪切并拼接")
    segments = build_segments(ordered_clauses, select_visual_fn)
    output = os.path.join(workdir, "deliverables", "final.mp4")
    render_video(media, segments, output, workdir,
                 width=render_size[0] if render_size else None,
                 height=render_size[1] if render_size else None,
                 preset=preset)

    manifest = {
        "source": media,
        "duration": timeline["duration"],
        "main_product": main_product,
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
        "segments": segments,
        "output": os.path.relpath(output, workdir),
        "transcript_words": len(words),
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
    return {"main_product": "主商品", "ordered_ids": chosen}


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
                        help="目标总时长，形如 45-60")
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
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--preset")
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
            asr_backend=args.backend,
            asr_model=args.model, transcript=transcript,
            render_size=(args.width, args.height) if args.width and args.height else None,
            preset=args.preset)
    except PipelineError as exc:
        print(f"[{exc.stage}] {exc}", file=sys.stderr)
        return 2
    print(f"成片：{os.path.join(args.workdir, manifest['output'])}")
    print(f"主商品：{manifest['main_product']}  总时长：{manifest['total_seconds']}s  "
          f"AI 调用：{manifest['ai_calls']} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
