"""V3-owned draft snapshot, validation, sentence mapping and original-media render.

Parser and DLL adapter were copied into this namespace; no V1/V2 business imports.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .draft_timeline import VirtualTimeline, discover_virtual_timelines, load_virtual_timeline, extract_virtual_timeline_audio
from .pipeline import command, duration


def validate(timeline):
    if not timeline.segments:
        raise ValueError("V3 草稿没有可用视频片段")
    previous = 0.
    source_durations = {}
    for seg in timeline.segments:
        values = (seg.timeline_start, seg.timeline_end, seg.source_start, seg.source_end, seg.speed)
        if not all(math.isfinite(v) for v in values) or seg.timeline_duration <= 0 or seg.source_start < 0 or seg.source_duration <= 0:
            raise ValueError("V3 草稿片段时间边界无效")
        if abs(seg.timeline_start - previous) > .002:
            raise ValueError("V3 当前只支持连续单主视频轨道：草稿存在空隙或重叠")
        ratio = seg.source_duration / seg.timeline_duration
        if not .125 <= ratio <= 8 or abs(ratio - seg.speed) > .01:
            raise ValueError("V3 只支持 0.125–8 倍恒定速度，源/草稿时长与倍速不一致")
        seg.speed = ratio
        source = Path(seg.source_path)
        if not source.is_file():
            raise ValueError(f"V3 草稿原素材不存在：{seg.source_path}")
        if seg.source_path not in source_durations:
            source_durations[seg.source_path] = float(command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(source)]))
        total = source_durations[seg.source_path]
        if seg.source_end > total + .05:
            raise ValueError(f"V3 草稿片段超出原素材时长：{seg.segment_id}")
        previous = seg.timeline_end
    return timeline


def list_timelines(path):
    return discover_virtual_timelines(path)


def snapshot(path):
    choices = list_timelines(path)["timelines"]
    selected = next((t for t in choices if t.get("selected")), None) or next((t for t in choices if t.get("active")), choices[0])
    timeline = validate(load_virtual_timeline(selected["path"]))
    return {"draft_path": str(Path(path).expanduser().resolve()), "selected_path": selected["path"],
            "source_stats": {p: {"size": Path(p).stat().st_size, "mtime_ns": Path(p).stat().st_mtime_ns} for p in timeline.source_paths},
            "timeline": timeline.to_dict(), "render_scope": "main_video_and_original_audio",
            "limitations": "不复现字幕、转场、特效、叠加轨和独立配音/音乐；输出 MP4，不回写原草稿"}


def understand(backend, data, workspace, transcript):
    for path, expected in data.get("source_stats", {}).items():
        stat = Path(path).stat()
        if {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns} != expected:
            raise ValueError("V3 草稿原素材已变化，请重新创建任务")
    timeline = validate(VirtualTimeline.from_dict(data["timeline"]))
    wav = extract_virtual_timeline_audio(timeline, workspace / "draft-audio.wav", workspace / "draft-audio")
    units = backend(wav, workspace, transcript)
    for unit in units:
        slices = timeline.map_timeline_range(unit["start"], unit["end"])
        if abs(sum(s["timeline_end"] - s["timeline_start"] for s in slices) - (unit["end"] - unit["start"])) > .005:
            raise ValueError("V3 完整句跨越未映射的草稿范围")
        unit["source_slices"] = slices
        unit["draft_timeline_id"] = timeline.timeline_id
    return units


def atempo(speed):
    factors = []
    while speed > 2:
        factors.append(2.)
        speed /= 2
    while speed < .5:
        factors.append(.5)
        speed *= 2
    factors.append(speed)
    return ",".join(f"atempo={f:.9g}" for f in factors)


def render(workspace, units):
    slices = [piece for unit in units for piece in unit["source_slices"]]
    if not slices:
        raise ValueError("V3 草稿成片没有映射片段")
    info = json.loads(command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "json", slices[0]["source_path"]]))["streams"][0]
    width, height = int(info["width"]) // 2 * 2, int(info["height"]) // 2 * 2
    parts = []
    for index, piece in enumerate(slices):
        streams = json.loads(command(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                     "-of", "json", piece["source_path"]]))["streams"]
        has_audio = any(s["codec_type"] == "audio" for s in streams)
        length = piece["timeline_end"] - piece["timeline_start"]
        args = ["ffmpeg", "-v", "error", "-y", "-ss", str(piece["source_start"]),
                "-t", str(piece["source_end"] - piece["source_start"]), "-i", piece["source_path"]]
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        video = f"setpts=(PTS-STARTPTS)/{piece['speed']:.9g},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
        part = workspace / f"draft-part-{index:04}.mp4"
        args += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", video,
                 "-af", atempo(piece["speed"]) + ",asetpts=PTS-STARTPTS,aresample=48000,apad",
                 "-t", str(length), "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", str(part)]
        command(args)
        parts.append(part)
    listing = workspace / "draft-concat.txt"
    listing.write_text("\n".join(f"file '{p.name}'" for p in parts), encoding="utf-8")
    output = workspace / "film.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(output)])
    actual = float(command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(output)]))
    if abs(actual - duration(units)) > max(.5, len(slices) * .1):
        raise ValueError("V3 草稿渲染时长与复审时间线不一致")
    return output
