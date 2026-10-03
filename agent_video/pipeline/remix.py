# -*- coding: utf-8 -*-
"""已发布成片集合的文字去重、动态分类与重排管线。

输入是一个剪映集合时间线；每个现有视频片段都是不可再拆的原子单元。该流程只
依据口播文字去重，不执行商品、违禁词或画面检查，也不会套固定卖点结构。
"""
from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any, Callable

from . import asr as asr_mod
from .ai import DEFAULT_TIMEOUT, ai_call
from .errors import AIReturnError, AsrError, PipelineError
from .render import build_virtual_segments, render_video
from .run import next_available_output

StageCallback = Callable[[str, str, str], None]

REMIX_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "role": {"type": "string", "minLength": 1, "maxLength": 40},
                    "ids": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "integer", "minimum": 0},
                    },
                },
                "required": ["role", "ids"],
            },
        },
        "ordered_ids": {
            "type": "array", "minItems": 1,
            "items": {"type": "integer", "minimum": 0},
        },
        "duplicate_ids": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
        },
    },
    "required": ["sections", "ordered_ids", "duplicate_ids"],
}

_NORMALIZE_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_SIMILARITY = {"lenient": 0.97, "standard": 0.92, "strict": 0.86}


def _emit(callback: StageCallback | None, stage: str, status: str, message: str) -> None:
    if callback is not None:
        callback(stage, status, message)


def _dump(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def _word_value(word: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in word:
            return word[key]
    return default


def build_clip_units(virtual_timeline: Any, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按集合时间线已有片段聚合 ASR 文字，保持片段边界不变。"""
    clips: list[dict[str, Any]] = []
    audio_cursor = 0.0
    ordered_segments = sorted(virtual_timeline.segments, key=lambda item: item.timeline_start)
    for index, segment in enumerate(ordered_segments):
        audio_start = audio_cursor
        audio_end = audio_start + segment.timeline_duration
        tokens: list[str] = []
        for word in words:
            try:
                start = float(_word_value(word, "s", "start", default=0.0))
                end = float(_word_value(word, "e", "end", default=start))
            except (TypeError, ValueError):
                continue
            midpoint = (start + end) / 2
            if audio_start <= midpoint < audio_end + (1e-6 if index == len(ordered_segments) - 1 else 0):
                text = str(_word_value(word, "w", "word", "text", default=""))
                if text:
                    tokens.append(text)
        clips.append({
            "id": index,
            "segment_id": segment.segment_id,
            "start": round(segment.timeline_start, 3),
            "end": round(segment.timeline_end, 3),
            "seconds": round(segment.timeline_duration, 3),
            "text": "".join(tokens).strip(),
            "usable": True,
            "reason": "",
            "category": "",
            "order": None,
        })
        audio_cursor = audio_end
    return clips


def _normalized(text: str) -> str:
    return _NORMALIZE_RE.sub("", text or "").lower()


def literal_deduplicate(clips: list[dict[str, Any]], strength: str = "standard") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """做确定性的字面近似去重；每组保留文字更完整的片段。"""
    threshold = _SIMILARITY.get(strength, _SIMILARITY["standard"])
    groups: list[list[dict[str, Any]]] = []
    empty: list[dict[str, Any]] = []
    for clip in clips:
        key = _normalized(str(clip.get("text") or ""))
        if not key:
            empty.append(clip)
            continue
        matched: list[dict[str, Any]] | None = None
        for group in groups:
            representative_key = _normalized(str(group[0].get("text") or ""))
            if key == representative_key or difflib.SequenceMatcher(
                    None, key, representative_key).ratio() >= threshold:
                matched = group
                break
        if matched is None:
            groups.append([clip])
        else:
            matched.append(clip)

    kept = list(empty)
    duplicates: list[dict[str, Any]] = []
    for group in groups:
        representative = max(
            group,
            key=lambda item: (len(_normalized(str(item.get("text") or ""))),
                              float(item.get("seconds") or 0.0),
                              -int(item.get("id") or 0)),
        )
        kept.append(representative)
        for item in group:
            if item is representative:
                continue
            item["usable"] = False
            item["reason"] = "literal_duplicate"
            item["duplicate_of"] = representative["id"]
            duplicates.append(item)
    kept.sort(key=lambda item: int(item["id"]))
    duplicates.sort(key=lambda item: int(item["id"]))
    return kept, duplicates


def _remix_prompt(clips: list[dict[str, Any]], strength: str) -> str:
    rows = [{"id": item["id"], "text": item["text"], "seconds": item["seconds"]}
            for item in clips]
    return (
        "你在重组同一个商品的多个已发布短视频。输入中的每条都是剪映里已经剪好的完整片段，"
        "商品与合规已由用户确认。只依据文字完成两件事：语义去重、动态分类重排。\n"
        "规则：\n"
        "1. 说法不同但信息点相同的片段只保留信息更完整的一条；仅有局部重叠但包含新增信息时必须保留。\n"
        "2. 不检查商品、违禁词、画面、价格或时效，不因这些原因删除。\n"
        "3. 根据实际素材自行命名分类，如穿搭、版型、材质、感受等；素材没有的分类不要补。\n"
        "4. 相近主题尽量相邻，保证衔接自然；没有固定钩子或卖点结构，不要求开头和收口。\n"
        "5. 不为控制时长删除独有信息，除重复外应全部保留。\n"
        "6. sections 按最终顺序输出，每个 id 只能出现一次；ordered_ids 必须等于 sections 中 ids 的展开；"
        "被语义去重的 id 只写入 duplicate_ids。\n"
        f"去重强度：{strength}。片段：\n"
        + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    )


def validate_remix_plan(plan: dict[str, Any], clips: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int], list[int]]:
    """验证模型没有伪造、遗漏或重复片段，并返回规范化结果。"""
    allowed = {int(item["id"]) for item in clips}
    sections = plan.get("sections")
    ordered = plan.get("ordered_ids")
    duplicates = plan.get("duplicate_ids")
    if not isinstance(sections, list) or not isinstance(ordered, list) or not isinstance(duplicates, list):
        raise AIReturnError("成片重组结果缺少 sections / ordered_ids / duplicate_ids")
    try:
        ordered_ids = [int(value) for value in ordered]
        duplicate_ids = [int(value) for value in duplicates]
    except (TypeError, ValueError) as exc:
        raise AIReturnError("成片重组结果包含非法片段 id") from exc
    if len(set(ordered_ids)) != len(ordered_ids) or len(set(duplicate_ids)) != len(duplicate_ids):
        raise AIReturnError("成片重组结果包含重复 id")
    if set(ordered_ids) & set(duplicate_ids):
        raise AIReturnError("同一片段不能同时保留和去重")
    if set(ordered_ids) | set(duplicate_ids) != allowed:
        raise AIReturnError("成片重组结果遗漏或伪造了片段 id")
    normalized_sections: list[dict[str, Any]] = []
    flattened: list[int] = []
    for section in sections:
        if not isinstance(section, dict):
            raise AIReturnError("成片重组分类格式无效")
        role = str(section.get("role") or "").strip()[:40]
        ids = section.get("ids")
        if not role or not isinstance(ids, list) or not ids:
            raise AIReturnError("成片重组分类名称或片段为空")
        try:
            section_ids = [int(value) for value in ids]
        except (TypeError, ValueError) as exc:
            raise AIReturnError("成片重组分类包含非法 id") from exc
        normalized_sections.append({"role": role, "ids": section_ids})
        flattened.extend(section_ids)
    if flattened != ordered_ids:
        raise AIReturnError("sections 展开顺序与 ordered_ids 不一致")
    if not ordered_ids:
        raise AIReturnError("文字去重后没有可保留片段")
    return normalized_sections, ordered_ids, duplicate_ids


def run_remix_pipeline(media: str, workdir: str, *, virtual_timeline: Any,
                       output_stem: str | None = None,
                       dedupe_strength: str = "standard",
                       ai_model: str | None = None,
                       ai_timeout: int = DEFAULT_TIMEOUT,
                       on_stage: StageCallback | None = None) -> dict[str, Any]:
    """运行集合时间线的文字去重、分类、重排与渲染。"""
    if virtual_timeline is None or not virtual_timeline.segments:
        raise PipelineError("成片重组需要包含有效片段的剪映集合时间线")
    if dedupe_strength not in _SIMILARITY:
        raise PipelineError("去重强度必须是 lenient、standard 或 strict")
    os.makedirs(workdir, exist_ok=True)
    model = ai_model or os.environ.get("PIPELINE_AI_MODEL") or "auto"

    _emit(on_stage, "asr", "start", "提取集合时间线音频并按现有片段转写")
    from ..timeline import extract_virtual_timeline_audio
    wav = extract_virtual_timeline_audio(
        virtual_timeline, os.path.join(workdir, "remix_audio.wav"), workdir)
    _sentences, words = asr_mod.transcribe(wav, workdir)
    clips = build_clip_units(virtual_timeline, words)
    if not clips:
        raise AsrError("集合时间线中没有可处理片段")
    timeline = {
        "source": virtual_timeline.title,
        "duration": round(float(virtual_timeline.total_duration), 3),
        "timeline_id": virtual_timeline.timeline_id,
        "workflow": "remix",
        "clauses": clips,
    }
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "clauses.json"), timeline)
    _emit(on_stage, "asr", "done", f"识别 {len(clips)} 个现有剪映片段的口播")

    _emit(on_stage, "filter", "start", "仅按文字执行字面近似去重")
    literal_kept, literal_duplicates = literal_deduplicate(clips, dedupe_strength)
    _dump(os.path.join(workdir, "clauses.filtered.json"), {
        "source": virtual_timeline.title,
        "duration": timeline["duration"],
        "workflow": "remix",
        "clauses": clips,
    })
    _emit(on_stage, "filter", "done",
          f"字面去重 {len(literal_duplicates)} 段，剩余 {len(literal_kept)} 段")

    text_candidates = [item for item in literal_kept if _normalized(str(item.get("text") or ""))]
    empty_candidates = [item for item in literal_kept if item not in text_candidates]
    semantic_duplicates: list[int] = []
    if text_candidates:
        _emit(on_stage, "judge", "start", "仅按口播文字执行语义去重与动态分类")
        raw_plan = ai_call(model, _remix_prompt(text_candidates, dedupe_strength),
                           REMIX_PLAN_SCHEMA, ai_timeout)
        sections, ordered_ids, semantic_duplicates = validate_remix_plan(
            raw_plan, text_candidates)
        _emit(on_stage, "judge", "done",
              f"语义去重 {len(semantic_duplicates)} 段，生成 {len(sections)} 个动态内容组")
    else:
        _emit(on_stage, "judge", "start", "集合时间线没有识别出可比较文字")
        sections, ordered_ids = [], []
        _emit(on_stage, "judge", "done", "无文字片段不参与去重")

    if empty_candidates:
        empty_ids = [int(item["id"]) for item in empty_candidates]
        sections.append({"role": "未识别文字", "ids": empty_ids})
        ordered_ids.extend(empty_ids)

    _emit(on_stage, "order", "start", "按动态内容分组生成新片顺序")
    by_id = {int(item["id"]): item for item in clips}
    category_by_id = {
        int(cid): section["role"] for section in sections for cid in section["ids"]
    }
    semantic_set = set(semantic_duplicates)
    order_by_id = {cid: index for index, cid in enumerate(ordered_ids)}
    for clip in clips:
        cid = int(clip["id"])
        if cid in semantic_set:
            clip["usable"] = False
            clip["reason"] = "semantic_duplicate"
        if cid in order_by_id:
            clip["usable"] = True
            clip["reason"] = ""
            clip["category"] = category_by_id.get(cid, "其他")
            clip["order"] = order_by_id[cid]
    ordered_clips = [by_id[cid] for cid in ordered_ids]
    total_seconds = round(sum(float(item["seconds"]) for item in ordered_clips), 3)
    timeline["clauses"] = clips
    _dump(os.path.join(workdir, "timeline.json"), timeline)
    _dump(os.path.join(workdir, "clauses.judged.json"), {
        "source": virtual_timeline.title,
        "duration": timeline["duration"],
        "workflow": "remix",
        "clauses": clips,
    })
    order_data = {
        "stage": "order", "workflow": "remix", "sections": sections,
        "ordered_ids": ordered_ids,
        "duplicate_ids": [int(item["id"]) for item in literal_duplicates] + semantic_duplicates,
        "total_seconds": total_seconds,
    }
    _dump(os.path.join(workdir, "order.json"), order_data)
    _dump(os.path.join(workdir, "remix_plan.json"), {
        **order_data,
        "dedupe_strength": dedupe_strength,
        "original_clip_count": len(clips),
        "kept_clip_count": len(ordered_clips),
        "literal_duplicate_count": len(literal_duplicates),
        "semantic_duplicate_count": len(semantic_duplicates),
        "clips": clips,
    })
    _emit(on_stage, "order", "done",
          f"按 {len(sections)} 个内容组重排 {len(ordered_clips)} 段，共 {total_seconds:.2f}s")

    _emit(on_stage, "render", "start", "按新顺序拼接原有剪映片段")
    segments = build_virtual_segments(ordered_clips, virtual_timeline)
    output = next_available_output(os.path.join(workdir, "deliverables"), output_stem)
    render_video(media, segments, output, workdir, virtual_timeline=virtual_timeline)
    manifest = {
        "source": virtual_timeline.title,
        "timeline_id": virtual_timeline.timeline_id,
        "workflow": "remix",
        "virtual_timeline": virtual_timeline.to_dict(),
        "duration": timeline["duration"],
        "total_seconds": total_seconds,
        "original_clip_count": len(clips),
        "kept_clip_count": len(ordered_clips),
        "duplicate_clip_count": len(clips) - len(ordered_clips),
        "order_sections": sections,
        "ai_calls": 1 if text_candidates else 0,
        "ai_engine": os.environ.get("PIPELINE_AI_ENGINE") or "llm",
        "ai_provider": os.environ.get("PIPELINE_AI_PROVIDER") or "auto",
        "ai_model": model,
        "segments": segments,
        "output": os.path.relpath(output, workdir),
        "transcript_words": len(words),
    }
    _dump(os.path.join(workdir, "manifest.json"), manifest)
    _emit(on_stage, "render", "done", f"成片重组完成：{manifest['output']}")
    return manifest
