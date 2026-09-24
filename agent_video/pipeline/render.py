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


def render_video(media: str, segments: list[dict[str, Any]], output: str, workdir: str,
                 *, width: int | None = None, height: int | None = None,
                 fps: float | None = None, preset: str | None = None) -> str:
    """调用 render_multi 把片段拼成 ``output``。"""
    if not segments:
        raise RenderError("渲染片段为空")
    timeline_path = os.path.join(workdir, "render_timeline.json")
    with open(timeline_path, "w", encoding="utf-8") as handle:
        json.dump([{"src": 1, "start": row["start"], "end": row["end"]}
                   for row in segments], handle, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    command = [sys.executable, RENDER_MULTI, timeline_path, os.path.abspath(output),
               "--src", f"1={os.path.abspath(media)}", "--force"]
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
