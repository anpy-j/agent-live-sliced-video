# -*- coding: utf-8 -*-
"""剪映虚拟时间线与双时间轴映射模块。

核心逻辑：
1. 剪映人工筛选出虚拟时间线（如 30-40 分钟）；
2. 系统保留两套时间轴：
   - 时间线时间 (timeline_time)：AI 与用户看到的连续内容；
   - 原素材时间 (source_time)：最终从原始素材读取画面与音频的位置；
3. S1 仅对虚拟时间线对应的片段提取音频拼接并做 ASR，被剪映排除的原片不会进入 ASR 与 AI；
4. AI 选中的时间线片段，在渲染时按片段与倍速精准换算回原视频位置并出片。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TimelineSegment:
    """时间线上的单个片段。"""

    segment_id: str           # 片段标识，如 "片段1" 或 uuid
    timeline_start: float     # 时间线起始秒数
    timeline_end: float       # 时间线结束秒数
    source_path: str          # 原始素材绝对路径
    source_start: float       # 原片起始秒数
    source_end: float         # 原片结束秒数
    speed: float = 1.0        # 播放倍速（原片时长 / 时间线时长）

    @property
    def timeline_duration(self) -> float:
        return max(0.0, self.timeline_end - self.timeline_start)

    @property
    def source_duration(self) -> float:
        return max(0.0, self.source_end - self.source_start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "timeline_start": round(self.timeline_start, 3),
            "timeline_end": round(self.timeline_end, 3),
            "source_path": self.source_path,
            "source_start": round(self.source_start, 3),
            "source_end": round(self.source_end, 3),
            "speed": round(self.speed, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimelineSegment:
        t_start = float(data["timeline_start"])
        t_end = float(data["timeline_end"])
        s_start = float(data["source_start"])
        s_end = float(data["source_end"])
        speed = float(data.get("speed") or 1.0)
        t_dur = t_end - t_start
        s_dur = s_end - s_start
        if speed <= 0 or (abs(speed - 1.0) < 1e-4 and t_dur > 0 and s_dur > 0 and abs(s_dur - t_dur) > 0.05):
            speed = s_dur / t_dur if t_dur > 0 else 1.0
        return cls(
            segment_id=str(data.get("segment_id", "片段")),
            timeline_start=t_start,
            timeline_end=t_end,
            source_path=os.path.abspath(str(data["source_path"])),
            source_start=s_start,
            source_end=s_end,
            speed=speed,
        )


@dataclass
class VirtualTimeline:
    """剪映虚拟时间线对象。"""

    timeline_id: str
    title: str = "虚拟时间线"
    segments: list[TimelineSegment] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        if not self.segments:
            return 0.0
        return max(seg.timeline_end for seg in self.segments)

    @property
    def timeline_duration(self) -> float:
        return self.total_duration

    @property
    def source_duration(self) -> float:
        return sum(seg.source_duration for seg in self.segments)

    @property
    def source_paths(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.source_path not in seen:
                seen.append(seg.source_path)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeline_id": self.timeline_id,
            "title": self.title,
            "total_duration": round(self.total_duration, 3),
            "timeline_duration": round(self.timeline_duration, 3),
            "source_duration": round(self.source_duration, 3),
            "segment_count": len(self.segments),
            "segments": [seg.to_dict() for seg in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VirtualTimeline:
        segments = [TimelineSegment.from_dict(item) for item in data.get("segments", [])]
        segments.sort(key=lambda s: s.timeline_start)
        return cls(
            timeline_id=str(data.get("timeline_id") or "时间线01"),
            title=str(data.get("title") or "虚拟时间线"),
            segments=segments,
        )

    def map_timeline_range(self, t_start: float, t_end: float) -> list[dict[str, Any]]:
        """把时间线上的 [t_start, t_end] 映射到原素材位置。

        返回列表（若跨片段则拆为多段），每项包含用户要求的两套时间规范：
        {
            "timeline_id": "时间线01",
            "timeline_start": 100,
            "timeline_end": 110,
            "segment_id": "片段1",
            "segment_offset": 100,
            "source_path": "...",
            "source_start": ...,
            "source_end": ...,
            "speed": 1.3
        }
        """
        slices: list[dict[str, Any]] = []
        if t_end <= t_start or not self.segments:
            return slices

        for seg in self.segments:
            if seg.timeline_end <= t_start or seg.timeline_start >= t_end:
                continue
            piece_start = max(t_start, seg.timeline_start)
            piece_end = min(t_end, seg.timeline_end)
            if piece_end <= piece_start:
                continue

            seg_offset = piece_start - seg.timeline_start
            src_start = seg.source_start + seg_offset * seg.speed
            src_dur = (piece_end - piece_start) * seg.speed
            src_end = src_start + src_dur

            slices.append({
                "timeline_id": self.timeline_id,
                "timeline_start": round(piece_start, 3),
                "timeline_end": round(piece_end, 3),
                "segment_id": seg.segment_id,
                "segment_offset": round(seg_offset, 3),
                "source_path": seg.source_path,
                "source_start": round(src_start, 3),
                "source_end": round(src_end, 3),
                "speed": round(seg.speed, 4),
            })
        return slices

    def map_clause(self, clause: dict[str, Any]) -> dict[str, Any]:
        """为单条子句注入双时间轴字段。"""
        c_start = float(clause["start"])
        c_end = float(clause["end"])
        slices = self.map_timeline_range(c_start, c_end)
        if slices:
            primary = slices[0]
            clause["timeline_id"] = primary["timeline_id"]
            clause["timeline_start"] = primary["timeline_start"]
            clause["timeline_end"] = primary["timeline_end"]
            clause["segment_id"] = primary["segment_id"]
            clause["segment_offset"] = primary["segment_offset"]
            clause["source_path"] = primary["source_path"]
            clause["source_start"] = primary["source_start"]
            clause["source_end"] = primary["source_end"]
            clause["speed"] = primary["speed"]
            clause["timeline_slices"] = slices
        else:
            clause["timeline_id"] = self.timeline_id
            clause["timeline_start"] = round(c_start, 3)
            clause["timeline_end"] = round(c_end, 3)
            clause["segment_id"] = "未知片段"
            clause["segment_offset"] = round(c_start, 3)
            clause["source_path"] = self.segments[0].source_path if self.segments else ""
            clause["source_start"] = round(c_start, 3)
            clause["source_end"] = round(c_end, 3)
            clause["speed"] = 1.0
            clause["timeline_slices"] = []
        return clause


def map_timeline_range(timeline: VirtualTimeline, t_start: float, t_end: float) -> list[dict[str, Any]]:
    """模块级辅助函数：将时间线范围映射到原素材位置。"""
    return timeline.map_timeline_range(t_start, t_end)


def map_clause(timeline: VirtualTimeline, clause: dict[str, Any]) -> dict[str, Any]:
    """模块级辅助函数：为单条子句注入双时间轴字段。"""
    return timeline.map_clause(clause)


def _parse_jianying_draft_dict(data: dict[str, Any], draft_dir: Path | None = None) -> VirtualTimeline:
    """解析剪映 draft_content.json 字典格式。"""
    materials = data.get("materials") or {}
    videos_list = materials.get("videos") or []
    speeds_list = materials.get("speeds") or []

    # 建立素材映射
    video_map: dict[str, dict[str, Any]] = {}
    for v in videos_list:
        vid = v.get("id")
        if vid:
            video_map[vid] = v

    speed_map: dict[str, float] = {}
    for s in speeds_list:
        sid = s.get("id")
        if sid and "speed" in s:
            try:
                speed_map[sid] = float(s["speed"])
            except (ValueError, TypeError):
                pass

    tracks = data.get("tracks") or []
    video_tracks = [t for t in tracks if t.get("type") == "video"]
    if not video_tracks:
        raise ValueError("剪映草稿中没有找到视频轨道 (tracks.type == 'video')")

    # 优先选取包含片段的主视频轨道
    main_track = video_tracks[0]
    for t in video_tracks:
        if t.get("segments"):
            main_track = t
            break

    raw_segments = main_track.get("segments") or []
    if not raw_segments:
        raise ValueError("剪映视频轨道中没有任何片段")

    segments: list[TimelineSegment] = []
    for idx, seg in enumerate(raw_segments):
        material_id = seg.get("material_id")
        video_material = video_map.get(material_id) or {}
        raw_path = video_material.get("path") or ""
        if not raw_path and draft_dir:
            # 检查是否有同名或 Resources 里的文件
            name = video_material.get("material_name") or ""
            if name and (draft_dir / name).is_file():
                raw_path = str(draft_dir / name)
            elif name and (draft_dir / "Resources" / name).is_file():
                raw_path = str(draft_dir / "Resources" / name)

        source_path = os.path.abspath(raw_path) if raw_path else ""

        # 剪映时间戳通常为微秒 (1秒 = 1,000,000 微秒)
        s_tr = seg.get("source_timerange") or {}
        t_tr = seg.get("target_timerange") or {}

        s_start_raw = float(s_tr.get("start", 0))
        s_dur_raw = float(s_tr.get("duration", 0))
        t_start_raw = float(t_tr.get("start", 0))
        t_dur_raw = float(t_tr.get("duration", 0))

        scale = 1_000_000.0 if (s_dur_raw > 10_000 or t_dur_raw > 10_000) else 1.0

        s_start = s_start_raw / scale
        s_dur = s_dur_raw / scale
        t_start = t_start_raw / scale
        t_dur = t_dur_raw / scale

        # 倍速计算
        speed = 1.0
        extra_refs = seg.get("extra_material_refs") or []
        for ref in extra_refs:
            if ref in speed_map:
                speed = speed_map[ref]
                break
        if abs(speed - 1.0) < 1e-4 and t_dur > 0 and s_dur > 0:
            speed = s_dur / t_dur

        seg_id = f"片段{idx + 1}"
        segments.append(TimelineSegment(
            segment_id=seg_id,
            timeline_start=round(t_start, 3),
            timeline_end=round(t_start + t_dur, 3),
            source_path=source_path,
            source_start=round(s_start, 3),
            source_end=round(s_start + s_dur, 3),
            speed=round(speed, 4),
        ))

    segments.sort(key=lambda s: s.timeline_start)
    title = draft_dir.name if draft_dir else "剪映虚拟时间线"
    timeline_id = f"时间线_{re.sub(r'[^a-zA-Z0-9_]+', '', title)[:24] or '01'}"
    return VirtualTimeline(timeline_id=timeline_id, title=title, segments=segments)


def load_virtual_timeline(target: str | Path | dict[str, Any]) -> VirtualTimeline:
    """从文件、目录或字典加载虚拟时间线。

    支持：
    1. 剪映 draft_content.json 或剪映草稿目录；
    2. 标准虚拟时间线 JSON 文件或字典格式。
    """
    if isinstance(target, dict):
        if "segments" in target and any("timeline_start" in s for s in target.get("segments", [])):
            return VirtualTimeline.from_dict(target)
        if "tracks" in target:
            return _parse_jianying_draft_dict(target)
        raise ValueError("无法识别的虚拟时间线字典结构")

    path = Path(target).expanduser().resolve()
    if path.is_dir():
        # 查找剪映草稿或时间线文件
        draft_content = path / "draft_content.json"
        if draft_content.is_file():
            path = draft_content
        else:
            candidates = list(path.glob("*timeline*.json")) + list(path.glob("*.json"))
            if candidates:
                path = candidates[0]
            else:
                raise FileNotFoundError(f"目录中未找到 draft_content.json 或时间线文件: {target}")

    if not path.is_file():
        raise FileNotFoundError(f"时间线文件不存在: {target}")

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        if (path.parent / "crypto_key_store.dat").is_file() or not content.strip().startswith(("{", "[")):
            raise ValueError(
                f"检测到剪映高版本对该草稿文件 ({path.name}) 启用了本地加密保护，外部程序无法直接读取。请在剪映中导出明文时间线/草稿，或使用直传视频模式。"
            ) from exc
        raise ValueError(f"时间线文件解析失败 ({path.name}): {exc}") from exc

    if isinstance(data, dict):
        if "segments" in data and any("timeline_start" in s for s in data.get("segments", [])):
            vt = VirtualTimeline.from_dict(data)
            if not vt.title or vt.title == "虚拟时间线":
                vt.title = path.stem
            return vt
        if "tracks" in data:
            return _parse_jianying_draft_dict(data, draft_dir=path.parent)

    raise ValueError(f"文件格式不符合剪映草稿或虚拟时间线规范: {path}")


def extract_virtual_timeline_audio(
    timeline: VirtualTimeline,
    output_wav: str | Path,
    workdir: str | Path | None = None,
) -> str:
    """提取虚拟时间线对应的各片段音频，并按顺序拼接为单一 16kHz mono WAV。

    保证：
    1. 只有剪映时间线保留的片段会进入音频，排除了原视频其余 7+ 小时；
    2. 音频时长与虚拟时间线总时长严格一致；
    3. 支持按片段倍速处理 (atempo)。
    """
    if not timeline.segments:
        raise ValueError("虚拟时间线没有任何片段，无法提取音频")

    output_wav = os.path.abspath(str(output_wav))
    os.makedirs(os.path.dirname(output_wav), exist_ok=True)
    if workdir is None:
        workdir = os.path.join(os.path.dirname(output_wav), "_vt_work")
    else:
        workdir = os.path.abspath(str(workdir))
    os.makedirs(workdir, exist_ok=True)

    manifest_path = output_wav + ".manifest.json"
    cache_key = {
        "timeline_id": timeline.timeline_id,
        "duration": round(timeline.total_duration, 3),
        "segments": [seg.to_dict() for seg in timeline.segments],
    }

    if os.path.isfile(output_wav) and os.path.isfile(manifest_path):
        try:
            stored_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            if stored_manifest.get("key") == cache_key:
                return output_wav
        except Exception:
            pass

    seg_dir = Path(workdir) / "timeline_audio_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_files: list[str] = []
    for idx, seg in enumerate(timeline.segments):
        if not os.path.isfile(seg.source_path):
            raise FileNotFoundError(f"片段 {seg.segment_id} 对应的原始素材文件不存在: {seg.source_path}")

        seg_wav = str(seg_dir / f"seg_{idx:04d}.wav")
        seg_duration = seg.source_duration
        if seg_duration <= 0.01:
            continue

        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{seg.source_start:.6f}",
            "-t", f"{seg_duration:.6f}",
            "-i", seg.source_path,
            "-vn",
        ]
        filters = []
        if abs(seg.speed - 1.0) > 1e-4:
            filters.append(f"atempo={seg.speed:.6f}")
        filters.extend([
            "aresample=16000",
            "aformat=sample_fmts=s16:channel_layouts=mono",
            "asetpts=PTS-STARTPTS",
        ])
        cmd += ["-filter:a", ",".join(filters), "-c:a", "pcm_s16le", seg_wav]

        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"提取片段 {seg.segment_id} 音频失败: {res.stderr.strip()[-500:]}")
        seg_files.append(seg_wav)

    if not seg_files:
        raise RuntimeError("没有成功提取出任何片段音频")

    # 拼接全部片段音频
    if len(seg_files) == 1:
        shutil.copy2(seg_files[0], output_wav)
    else:
        concat_list_file = Path(workdir) / "timeline_concat_list.txt"
        with open(concat_list_file, "w", encoding="utf-8") as f:
            for sf in seg_files:
                # 兼容 Windows 路径安全写法
                safe_path = sf.replace("\\", "/")
                f.write(f"file '{safe_path}'\n")

        concat_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list_file),
            "-c", "copy",
            output_wav,
        ]
        res = subprocess.run(concat_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"拼接时间线音频失败: {res.stderr.strip()[-500:]}")

    Path(manifest_path).write_text(json.dumps({"key": cache_key}, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_wav
