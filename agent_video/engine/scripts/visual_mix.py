# -*- coding: utf-8 -*-
"""Prepare and apply a conservative multimodal B-roll replacement plan."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

try:
    from .frames import build_overview, grab, timestamp_label
except ImportError:  # direct script execution
    from frames import build_overview, grab, timestamp_label


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    return float(result.stdout.strip())


def scene_boundaries(path: Path, duration: float, threshold: float = 0.3,
                     start: float = 0.0, end: float | None = None) -> list[float]:
    """Detect shot boundaries only inside the requested window, including both ends."""
    scan_start = max(0.0, float(start))
    scan_end = min(float(duration), float(end) if end is not None else float(duration))
    if scan_end <= scan_start:
        return [scan_start, scan_end]
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "info", "-ss", f"{scan_start:.3f}",
         "-t", f"{scan_end - scan_start:.3f}", "-i", str(path), "-an",
         "-vf", f"setpts=PTS-STARTPTS,select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    times = [scan_start]
    for line in result.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            value = scan_start + float(line.split("pts_time:", 1)[1].split()[0])
        except (IndexError, ValueError):
            continue
        if scan_start + 0.1 < value < scan_end - 0.1 and value - times[-1] > 0.15:
            times.append(value)
    times.append(scan_end)
    return times


def crosses_scene(start: float, end: float, boundaries: list[float]) -> bool:
    return any(start + 0.12 < point < end - 0.12 for point in boundaries[1:-1])


def sample_times(start: float, end: float, duration: float) -> list[float]:
    margin = min(0.12, max(0.0, (end - start) / 6))
    points = [start + margin, (start + end) / 2, end - margin]
    return [min(max(0.0, point), max(0.0, duration - 0.05)) for point in points]


def quality(path: Path) -> dict[str, float | bool | list[str]]:
    image = Image.open(path).convert("L").resize((240, 426))
    stat = ImageStat.Stat(image)
    brightness = float(stat.mean[0])
    contrast = float(stat.stddev[0])
    sharpness = float(ImageStat.Stat(image.filter(ImageFilter.FIND_EDGES)).mean[0])
    reasons = []
    if brightness < 18:
        reasons.append("画面接近黑屏")
    elif brightness > 245:
        reasons.append("画面严重过曝")
    if contrast < 6:
        reasons.append("画面几乎无有效细节")
    if sharpness < 2.5:
        reasons.append("画面严重模糊")
    return {"brightness": round(brightness, 2), "contrast": round(contrast, 2),
            "sharpness": round(sharpness, 2), "bad": bool(reasons), "reasons": reasons}


def segment_quality(paths: list[Path], *, scene_cut: bool = False) -> dict:
    samples = [quality(path) for path in paths]
    reasons = sorted({reason for item in samples for reason in item["reasons"]})
    if scene_cut:
        reasons.append("片段跨越镜头边界")
    return {
        "brightness": round(sum(float(item["brightness"]) for item in samples) / len(samples), 2),
        "contrast": round(min(float(item["contrast"]) for item in samples), 2),
        "sharpness": round(min(float(item["sharpness"]) for item in samples), 2),
        "bad": bool(reasons), "reasons": reasons, "samples": len(samples),
    }


def content_center(paths: list[Path]) -> float:
    """Estimate a stable horizontal crop center from edge-rich foreground content."""
    scores = [0.0] * 320
    height = 180
    for path in paths:
        image = Image.open(path).convert("L").resize((320, height)).filter(ImageFilter.FIND_EDGES)
        pixels = list(image.getdata())
        for y in range(8, height - 8):
            row = y * 320
            for x in range(320):
                # A soft center prior avoids snapping to text/UI at the extreme edge.
                scores[x] += pixels[row + x] * (0.65 + 0.35 * (1 - abs(x - 159.5) / 160))
    window = max(36, int(height * 9 / 16))
    running = sum(scores[:window])
    best_score, best_left = running, 0
    for left in range(1, 320 - window + 1):
        running += scores[left + window - 1] - scores[left - 1]
        if running > best_score:
            best_score, best_left = running, left
    center = (best_left + window / 2) / 320
    return round(min(0.92, max(0.08, center)), 4)


def inspect_segment(source: Path, start: float, end: float, duration: float,
                    target_dir: Path, stem: str,
                    boundaries: list[float] | None = None) -> tuple[dict, float, Path]:
    boundaries = boundaries or scene_boundaries(source, duration, start=start, end=end)
    frames = []
    for suffix, at in zip(("start", "middle", "end"), sample_times(start, end, duration)):
        frame = target_dir / f"{stem}-{suffix}.jpg"
        grab(str(source), at, str(frame))
        frames.append(frame)
    metrics = segment_quality(frames, scene_cut=crosses_scene(start, end, boundaries))
    return metrics, content_center(frames), frames[1]


def ranked_candidates(block: dict, candidates: list[dict], limit: int = 6) -> list[dict]:
    """Require known product/color matches before temporal proximity."""
    center = (float(block["start"]) + float(block["end"])) / 2
    product, color = block.get("product", ""), block.get("color", "")
    pool = ([item for item in candidates if item.get("product") == product]
            if product else list(candidates))
    if color:
        pool = [item for item in pool if item.get("color") == color]
    return sorted(pool, key=lambda item: (
        abs((float(item["start"]) + float(item["end"])) / 2 - center),
    ))[:limit]


def prepare(source: Path, mapping_path: Path, output_dir: Path,
            interval: float, max_candidates: int,
            label_anchors_path: Path | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping = read_json(mapping_path)
    timelines = {name: Path(path) for name, path in (mapping.get("dual_timelines") or {}).items()}
    if not timelines:
        raise RuntimeError("没有可用于混剪的双轨时间线")
    duration = media_duration(source)
    selected_dir = output_dir / "selected"
    candidate_dir = output_dir / "candidates"
    selected_dir.mkdir(exist_ok=True)
    candidate_dir.mkdir(exist_ok=True)
    blocks, selected_images = [], []
    for module, timeline_path in sorted(timelines.items()):
        for index, block in enumerate(read_json(timeline_path)):
            audio = block.get("audio") or {}
            start, end = float(audio["start"]), float(audio["end"])
            metrics, crop_x, frame = inspect_segment(
                source, start, end, duration, selected_dir, f"{module}-{index:03d}")
            block_id = f"{module}:{index}"
            blocks.append({"block_id": block_id, "module": module, "index": index,
                           "text": str(audio.get("text", "")), "start": start, "end": end,
                           "product": str(audio.get("product", "")),
                           "color": str(audio.get("color", "")),
                           "duration": round(end - start, 3), "quality": metrics,
                           "crop_x": crop_x})
            for suffix, at in zip(("start", "middle", "end"), sample_times(start, end, duration)):
                selected_images.append(
                    (f"{block_id} {suffix} {timestamp_label(at)}",
                     str(selected_dir / f"{module}-{index:03d}-{suffix}.jpg")))
    reference = output_dir / "selected-reference.jpg"
    build_overview(selected_images, str(reference))

    # Every selected segment is reviewed semantically.  Local pixel metrics cannot
    # detect empty sets, a missing presenter, wardrobe changes, or the wrong product.
    replacement_blocks = list(blocks)

    candidates, good_images = [], []
    label_anchors = []
    if label_anchors_path and label_anchors_path.is_file():
        raw_anchors = read_json(label_anchors_path)
        if isinstance(raw_anchors, list):
            label_anchors.extend(item for item in raw_anchors if isinstance(item, dict))
    label_anchors.extend({"time": (float(item["start"]) + float(item["end"])) / 2,
                          "product": item.get("product", ""),
                          "color": item.get("color", "")}
                         for item in blocks if item.get("product") or item.get("color"))
    longest = max(float(item["duration"]) for item in blocks)
    # Search only around the bad selected clips.  A full-source scene scan was the
    # dominant cost (minutes on a 16-minute 4K source) even when no replacement was needed.
    starts: list[float] = []
    for block in replacement_blocks:
        left = max(0.0, float(block["start"]) - 90.0)
        right = min(duration, float(block["end"]) + 90.0)
        step = max(float(interval), longest)
        cursor = left
        while cursor + longest <= right + 1e-6:
            if all(abs(cursor - existing) >= longest for existing in starts):
                starts.append(cursor)
            cursor += step
    starts.sort()
    if len(starts) > max_candidates:
        stride = len(starts) / max_candidates
        starts = [starts[min(len(starts) - 1, int(index * stride))]
                  for index in range(max_candidates)]
    for index, start in enumerate(starts):
        candidate_id = f"C{index:03d}"
        end = min(duration, start + longest)
        metrics, crop_x, frame = inspect_segment(
            source, start, end, duration, candidate_dir, candidate_id)
        if metrics["bad"]:
            continue
        center = (start + end) / 2
        nearest = (min(label_anchors, key=lambda item: abs(float(item["time"]) - center))
                   if label_anchors else {})
        candidates.append({"candidate_id": candidate_id, "start": round(start, 3),
                           "end": round(end, 3), "quality": metrics, "crop_x": crop_x,
                           "product": nearest.get("product", ""),
                           "color": nearest.get("color", "")})
        good_images.append((f"{candidate_id}  {timestamp_label(start)}", str(frame)))
    candidate_sets = {}
    allowed_ids = set()
    for block in replacement_blocks:
        nearby = ranked_candidates(block, candidates)
        candidate_sets[block["block_id"]] = [item["candidate_id"] for item in nearby]
        allowed_ids.update(candidate_sets[block["block_id"]])
    visible_images = [item for item in good_images if item[0].split()[0] in allowed_ids]
    sheets = []
    for offset in range(0, len(visible_images), 24):
        sheet = output_dir / f"candidate-sheet-{offset // 24 + 1:02d}.jpg"
        build_overview(visible_images[offset:offset + 24], str(sheet))
        sheets.append(str(sheet.resolve()))
    packet = {
        "source": str(source.resolve()), "duration": round(duration, 3),
        "reference_image": str(reference.resolve()), "candidate_sheets": sheets,
        "blocks": blocks, "replacement_blocks": replacement_blocks,
        "candidate_sets": candidate_sets,
        "candidates": [item for item in candidates if item["candidate_id"] in allowed_ids],
        "search_strategy": "full_selected_visual_audit_nearby_windows",
    }
    write_json_atomic(output_dir / "visual_mix_packet.json", packet)
    return packet


def _overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Overlap length relative to the shorter clip; 1.0 means one contains the other."""
    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap <= 0:
        return 0.0
    shorter = max(1e-6, min(a_end - a_start, b_end - b_start))
    return overlap / shorter


def apply(packet_path: Path, decisions_path: Path, mapping_path: Path,
          report_path: Path) -> dict:
    packet, decisions, mapping = read_json(packet_path), read_json(decisions_path), read_json(mapping_path)
    needed = {item["block_id"]: item for item in packet.get("replacement_blocks", [])}
    candidates = {item["candidate_id"]: item for item in packet.get("candidates", [])}
    accepted, ignored = [], []
    by_module: dict[str, list[dict]] = {}
    for item in decisions.get("replacements", []):
        block_id, candidate_id = str(item.get("block_id", "")), str(item.get("candidate_id", ""))
        candidate_sets = packet.get("candidate_sets") or {}
        allowed = set(candidate_sets.get(block_id, []))
        if (block_id not in needed or candidate_id not in candidates
                or (candidate_sets and candidate_id not in allowed)):
            ignored.append({"block_id": block_id, "candidate_id": candidate_id,
                            "reason": "未知片段或候选"})
            continue
        if item.get("mouth_visibility") == "clear":
            ignored.append({"block_id": block_id, "candidate_id": candidate_id,
                            "reason": "正面嘴部清晰，异时原声会形成假口型"})
            continue
        module, raw_index = block_id.rsplit(":", 1)
        by_module.setdefault(module, []).append({**item, "index": int(raw_index)})
    blocks = {item["block_id"]: item for item in packet.get("blocks", [])}
    # 画面不重复是硬约束：同一候选镜头、或与任何已成片画面（含原声 A-roll）
    # 重叠过半的范围，都只允许出现一次。跨模块全局去重，钩子和正文共用同一份记录。
    used_candidate_ids: set[str] = set()
    used_ranges: list[tuple[int, float, float]] = []
    for item in blocks.values():
        used_ranges.append((int(item.get("src", 1) or 1),
                            float(item.get("start", 0)), float(item.get("end", 0))))
    for module, timeline_value in mapping.get("dual_timelines", {}).items():
        changes = by_module.get(module, [])
        timeline_path = Path(timeline_value)
        rows = read_json(timeline_path)
        for index, row in enumerate(rows):
            crop_x = float(blocks.get(f"{module}:{index}", {}).get("crop_x", 0.5))
            # Rebuild the synchronized A-roll baseline on every apply.  Otherwise a retry
            # with an empty/rejected decision can silently keep an older B-roll choice.
            audio = row.get("audio") or {}
            row["video"] = [{"src": int(audio.get("src", 1)),
                             "start": float(audio.get("start", 0)),
                             "end": float(audio.get("end", 0)),
                             "kind": "aroll", "crop_x": crop_x}]
        for change in changes:
            index = change["index"]
            if not 0 <= index < len(rows):
                ignored.append({**change, "reason": "片段序号越界"})
                continue
            block = needed[f"{module}:{index}"]
            candidate = candidates[str(change["candidate_id"])]
            clip_end = float(candidate["start"]) + float(block["duration"])
            if clip_end > float(packet["duration"]) + 0.001:
                ignored.append({**change, "reason": "候选画面长度不足"})
                continue
            clip_start = float(candidate["start"])
            if change["candidate_id"] in used_candidate_ids:
                ignored.append({**change, "reason": "同一镜头已被其他片段使用，画面不得重复"})
                continue
            collision = next(((src, s, e) for src, s, e in used_ranges
                              if src == 1 and _overlap_ratio(clip_start, clip_end, s, e) >= 0.5), None)
            if collision:
                ignored.append({**change, "reason": "替换画面与成片中已有画面重复"})
                continue
            used_candidate_ids.add(str(change["candidate_id"]))
            used_ranges.append((1, clip_start, clip_end))
            rows[index]["video"] = [{"src": 1, "start": clip_start,
                                     "end": round(clip_end, 3), "kind": "broll",
                                     "candidate_id": change["candidate_id"],
                                     "crop_x": float(candidate.get("crop_x", 0.5))}]
            accepted.append({"block_id": f"{module}:{index}",
                             "candidate_id": change["candidate_id"],
                             "reason": str(change.get("reason", "")),
                             "shot_type": str(change.get("shot_type", "")),
                             "mouth_visibility": str(change.get("mouth_visibility", ""))})
        write_json_atomic(timeline_path, rows)
    report = {"ok": True, "requested": len(needed), "replaced": len(accepted),
              "kept_original": len(needed) - len(accepted), "accepted": accepted,
              "ignored": ignored,
              "duplicate_shots_rejected": sum(
                  1 for item in ignored if "重复" in str(item.get("reason", "")))}
    write_json_atomic(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("source")
    prep.add_argument("mapping")
    prep.add_argument("output_dir")
    prep.add_argument("--interval", type=float, default=10.0)
    prep.add_argument("--max-candidates", type=int, default=24)
    prep.add_argument("--label-anchors")
    use = sub.add_parser("apply")
    use.add_argument("packet")
    use.add_argument("decisions")
    use.add_argument("mapping")
    use.add_argument("report")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(Path(args.source), Path(args.mapping), Path(args.output_dir),
                         args.interval, args.max_candidates,
                         Path(args.label_anchors) if args.label_anchors else None)
        print(json.dumps({"replacement_blocks": len(result["replacement_blocks"]),
                          "candidates": len(result["candidates"])}, ensure_ascii=False))
    else:
        print(json.dumps(apply(Path(args.packet), Path(args.decisions), Path(args.mapping),
                               Path(args.report)), ensure_ascii=False))


if __name__ == "__main__":
    main()
