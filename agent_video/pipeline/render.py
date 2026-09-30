# -*- coding: utf-8 -*-
"""S6 渲染：按子句自身 ``[start, end]`` 逐段剪切并拼接（确定性）。

复用引擎既有的生产渲染原语 ``engine/scripts/render_multi.py``（内部再用
``ffmpeg_graph`` 落 filter graph 文件），确保输出参数与旧成片一致。

步骤 5（根据文本选择对应画面 / 换画面）的唯一接缝是 ``select_visual``：
默认实现返回子句自身的时间范围。新逻辑只需替换传给 ``run_pipeline`` 的
``select_visual_fn``，渲染层不需要改动。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .errors import RenderError

RENDER_MULTI = str(Path(__file__).resolve().parents[1] / "engine" / "scripts" / "render_multi.py")

SelectVisual = Callable[[dict[str, Any]], "tuple[float, float]"]


def select_visual(clause: dict[str, Any]) -> tuple[float, float]:
    """MVP 实现：画面的时间范围就是子句自身的时间范围。"""
    return float(clause["start"]), float(clause["end"])


def build_segments(ordered_clauses: list[dict[str, Any]],
                   select_visual_fn: SelectVisual | None = None
                   ) -> list[dict[str, Any]]:
    """把排序后的子句映射成渲染片段，保留文本与来源时间。"""
    selector = select_visual_fn or select_visual
    segments: list[dict[str, Any]] = []
    for clause in ordered_clauses:
        start, end = selector(clause)
        start, end = float(start), float(end)
        if end <= start:
            raise RenderError(
                f"子句 {clause.get('id')} 的画面范围非法：{start}-{end}")
        segments.append({
            "id": clause["id"],
            "start": round(start, 3),
            "end": round(end, 3),
            "text": clause.get("text", ""),
            "source_start": round(float(clause["start"]), 3),
            "source_end": round(float(clause["end"]), 3),
        })
    return segments

def build_virtual_segments(ordered_clauses: list[dict[str, Any]],
                           virtual_timeline: Any) -> list[dict[str, Any]]:
    """根据虚拟时间线与双时间轴映射，构建渲染片段列表。"""
    segments: list[dict[str, Any]] = []
    for clause in ordered_clauses:
        c_start = float(clause["start"])
        c_end = float(clause["end"])
        slices = virtual_timeline.map_timeline_range(c_start, c_end)
        for piece in slices:
            segments.append({
                "id": clause["id"],
                "timeline_id": piece["timeline_id"],
                "timeline_start": piece["timeline_start"],
                "timeline_end": piece["timeline_end"],
                "segment_id": piece["segment_id"],
                "segment_offset": piece["segment_offset"],
                "source_path": piece["source_path"],
                "source_start": piece["source_start"],
                "source_end": piece["source_end"],
                "speed": piece.get("speed", 1.0),
                "start": piece["source_start"],
                "end": piece["source_end"],
                "text": clause.get("text", ""),
            })
    if not segments:
        raise RenderError("虚拟时间线映射后渲染片段为空")
    return segments


def group_segments_for_export(
        segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group adjacent physical slices that belong to one selected AI clause.

    A virtual-timeline clause can cross Jianying cut boundaries and is therefore
    flattened into several render rows.  Split export must concatenate those rows
    into one deliverable instead of trying to encode millisecond-sized fragments.
    """
    groups: list[list[dict[str, Any]]] = []
    for segment in segments:
        segment_id = segment.get("id")
        if groups and segment_id is not None and groups[-1][-1].get("id") == segment_id:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def _run_render_multi(rows: list[dict[str, Any]], output: str, workdir: str,
                      timeline_name: str, *, media: str | None = None,
                      src_args: list[str] | None = None,
                      no_loudnorm: bool = False,
                      width: int | None = None, height: int | None = None,
                      fps: float | None = None, preset: str | None = None) -> str:
    timeline_path = os.path.join(workdir, timeline_name)
    with open(timeline_path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    if src_args is None:
        if not media:
            raise RenderError("必须提供 media 或 src_args")
        src_args = ["--src", f"1={os.path.abspath(media)}"]
    command = [sys.executable, RENDER_MULTI, timeline_path, os.path.abspath(output),
               *src_args, "--force"]
    if no_loudnorm:
        command.append("--no-loudnorm")
    if width and height:
        command += ["--width", str(width), "--height", str(height)]
    if fps:
        command += ["--fps", str(fps)]
    if preset:
        command += ["--preset", preset]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        raise RenderError(f"ffmpeg 渲染失败：{result.stderr.strip()[-1200:]}")
    if not os.path.isfile(output):
        raise RenderError(f"ffmpeg 结束但没有产出成片：{output}")
    return output


def render_video(media: str, segments: list[dict[str, Any]], output: str, workdir: str,
                 *, width: int | None = None, height: int | None = None,
                 fps: float | None = None, preset: str | None = None,
                 virtual_timeline: Any | None = None) -> str:
    """调用 render_multi 把片段拼成单个 ``output``。支持单素材直接渲染与虚拟时间线多源映射渲染。"""
    if not segments:
        raise RenderError("渲染片段为空")

    has_custom_sources = any("source_path" in row and row["source_path"] for row in segments)
    if has_custom_sources or virtual_timeline:
        source_paths: list[str] = []
        for row in segments:
            p = row.get("source_path") or media
            if p and p not in source_paths:
                source_paths.append(p)
        if not source_paths and media:
            source_paths.append(media)

        source_to_id = {path: idx + 1 for idx, path in enumerate(source_paths)}
        timeline_rows = []
        for row in segments:
            src_path = row.get("source_path") or media
            src_id = source_to_id.get(src_path, 1)
            start = float(row.get("source_start", row["start"]))
            end = float(row.get("source_end", row["end"]))
            speed = float(row.get("speed", 1.0))
            timeline_rows.append({"src": src_id, "start": start, "end": end, "speed": speed})

        src_args: list[str] = []
        for path, src_id in source_to_id.items():
            src_args.extend(["--src", f"{src_id}={os.path.abspath(path)}"])

        return _run_render_multi(timeline_rows, output, workdir, "render_timeline.json",
                                 src_args=src_args, width=width, height=height,
                                 fps=fps, preset=preset)
    else:
        rows = [{"src": 1, "start": row["start"], "end": row["end"]} for row in segments]
        return _run_render_multi(rows, output, workdir, "render_timeline.json",
                                 media=media, width=width, height=height, fps=fps, preset=preset)


def render_segment(media: str, segment: dict[str, Any] | list[dict[str, Any]],
                   output: str, workdir: str,
                   *, width: int | None = None, height: int | None = None,
                   fps: float | None = None, preset: str | None = None,
                   index: int = 0) -> str:
    """单独剪出 ``segment``；保持源响度，不做整条 loudnorm。"""
    items = segment if isinstance(segment, list) else [segment]
    if not items:
        raise RenderError("导出分段为空")

    if len(items) > 1:
        source_paths: list[str] = []
        for item in items:
            path = str(item.get("source_path") or media)
            if path not in source_paths:
                source_paths.append(path)
        source_to_id = {path: source_id for source_id, path in enumerate(source_paths, 1)}
        rows = []
        for item in items:
            path = str(item.get("source_path") or media)
            row = {
                "src": source_to_id[path],
                "start": float(item.get("source_start", item["start"])),
                "end": float(item.get("source_end", item["end"])),
            }
            if "speed" in item:
                row["speed"] = float(item["speed"])
            rows.append(row)
        src_args: list[str] = []
        for path, source_id in source_to_id.items():
            src_args.extend(["--src", f"{source_id}={os.path.abspath(path)}"])
        return _run_render_multi(
            rows, output, workdir, f"render_segment_{index:03d}.json",
            src_args=src_args, no_loudnorm=True, width=width, height=height,
            fps=fps, preset=preset,
        )

    segment = items[0]
    # Virtual-timeline jobs store a human-readable label in the job-level
    # source_path.  Each mapped segment carries the real media path and the
    # source-axis timestamps, which must take precedence for split export.
    segment_media = str(segment.get("source_path") or media)
    start = float(segment.get("source_start", segment["start"]))
    end = float(segment.get("source_end", segment["end"]))
    row = {"src": 1, "start": start, "end": end}
    if "speed" in segment:
        row["speed"] = float(segment["speed"])
    rows = [row]
    return _run_render_multi(rows, output, workdir,
                             f"render_segment_{index:03d}.json", media=segment_media,
                             no_loudnorm=True, width=width, height=height, fps=fps, preset=preset)
