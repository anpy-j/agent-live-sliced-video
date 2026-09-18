# -*- coding: utf-8 -*-
"""Validate the original-video mapping without printing the full timeline."""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from badvocab import hit  # noqa: E402

def parse_sources(values):
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.isdigit() or not os.path.isfile(path):
            raise SystemExit(f"Invalid --src: {value!r}; expected ID=existing-media")
        result[int(key)] = os.path.abspath(path)
    return result


def duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    return float(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("timeline")
    parser.add_argument("--src", action="append", default=[])
    parser.add_argument("--report", required=True)
    parser.add_argument("--min-total", type=float, default=90.0)
    parser.add_argument("--max-total", type=float, default=120.0)
    args = parser.parse_args()
    rows = json.load(open(args.timeline, encoding="utf-8"))
    sources = parse_sources(args.src)
    source_durations = {key: duration(path) for key, path in sources.items()}
    issues, hook_total, total = [], 0.0, 0.0

    def add(code, block, **extra):
        issues.append({"code": code, "block": block, **extra})

    for block_index, block in enumerate(rows):
        audio = block.get("audio") or {}
        section = block.get("section", "body")
        audio_src = int(audio.get("src", -1))
        audio_start, audio_end = float(audio.get("start", -1)), float(audio.get("end", -1))
        audio_duration = audio_end - audio_start
        if audio_src not in sources:
            add("missing_audio_source", block_index, src=audio_src)
        elif audio_start < 0 or audio_end <= audio_start or \
                audio_end > source_durations[audio_src] + 0.05:
            add("audio_out_of_bounds", block_index, src=audio_src)
        banned = hit(audio.get("text", ""))
        if banned:
            add("banned_word", block_index, match=banned)
        total += max(0.0, audio_duration)
        if section == "hook":
            hook_total += max(0.0, audio_duration)
        videos = block.get("video") or []
        if not videos:
            add("missing_video", block_index)
            continue
        video_total = 0.0
        remaining = max(0.0, audio_duration)
        for piece_index, piece in enumerate(videos):
            video_src = int(piece.get("src", -1))
            start, end = float(piece.get("start", -1)), float(piece.get("end", -1))
            raw_duration = end - start
            audio_offset = audio_duration - remaining
            used = min(max(0.0, raw_duration), remaining)
            remaining -= used
            video_total += max(0.0, raw_duration)
            if video_src not in sources:
                add("missing_video_source", block_index, piece=piece_index, src=video_src)
            elif start < 0 or end <= start or end > source_durations[video_src] + 0.05:
                add("video_out_of_bounds", block_index, piece=piece_index, src=video_src)
            if piece.get("kind", "aroll") != "aroll":
                add("unexpected_video_kind", block_index, piece=piece_index,
                    kind=piece.get("kind"))
            expected_start = audio_start + audio_offset
            if video_src != audio_src or abs(start - expected_start) > 0.12:
                add("aroll_not_audio_aligned", block_index, piece=piece_index,
                    expected_src=audio_src, expected_start=round(expected_start, 3))
        if video_total + 0.001 < audio_duration:
            add("video_too_short", block_index, audio=round(audio_duration, 3),
                video=round(video_total, 3))
        if video_total - audio_duration > 2.0:
            add("excess_video", block_index, surplus=round(video_total - audio_duration, 3))

    if total < args.min_total or total > args.max_total:
        issues.append({"code": "duration_range", "duration": round(total, 3),
                       "expected": [args.min_total, args.max_total]})
    if hook_total > 8.05:
        issues.append({"code": "hook_too_long", "duration": round(hook_total, 3),
                       "maximum": 8.0})
    report = {"ok": not issues, "blocks": len(rows), "duration": round(total, 3),
              "mode": "original_video", "issues": issues}
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"video mapping {'passed' if not issues else 'failed'}: {len(rows)} blocks, "
          f"{total:.2f}s, {len(issues)} issues -> {args.report}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
