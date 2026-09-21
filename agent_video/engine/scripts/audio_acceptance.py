"""Render a cheap speech-only preview and hard-gate every audio junction."""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asr_backend import Transcriber  # noqa: E402


def norm(value: str) -> str:
    return "".join(char.lower() for char in value
                   if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def load_word_map(path: Path) -> dict[int, list[dict]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for key, value in manifest.items():
        if str(key).startswith("_"):
            continue
        target = Path(value)
        if not target.is_absolute():
            target = path.parent / target
        result[int(key)] = json.loads(target.read_text(encoding="utf-8"))
    return result


def analyze_junctions(rows: list[dict], words_by_source: dict[int, list[dict]]) -> dict:
    segments, issues, junctions = [], [], []
    for index, row in enumerate(rows):
        source = int(row.get("src", 1))
        start, end = float(row["start"]), float(row["end"])
        words = words_by_source.get(source, [])
        inside = [word for word in words
                  if float(word["e"]) > start + 1e-6 and float(word["s"]) < end - 1e-6]
        partial = [word for word in inside
                   if float(word["s"]) < start - 0.005 or float(word["e"]) > end + 0.005]
        actual = "".join(str(word.get("w") or "") for word in inside)
        expected = str(row.get("text") or "")
        similarity = (difflib.SequenceMatcher(None, norm(expected), norm(actual)).ratio()
                      if norm(expected) else 1.0)
        lead = (float(inside[0]["s"]) - start) if inside else end - start
        tail = (end - max(float(word["e"]) for word in inside)) if inside else end - start
        segment = {"segment": index, "planned_text": expected, "actual_asr": actual,
                   "similarity": round(similarity, 4), "head_silence": round(lead, 3),
                   "tail_silence": round(tail, 3), "partial_tokens": [
                       str(word.get("w") or "") for word in partial]}
        segments.append(segment)
        if partial:
            issues.append({"code": "cut_inside_token", "segment": index,
                           "detail": "切点落在词内：" + "".join(segment["partial_tokens"])})
        if similarity < 0.86:
            issues.append({"code": "source_asr_mismatch", "segment": index,
                           "detail": f"源 ASR 与计划口播相似度仅 {similarity:.2f}"})
        if lead > 0.35:
            issues.append({"code": "abnormal_head_pause", "segment": index,
                           "detail": f"句首空白 {lead:.2f}s"})
        if tail > 0.40:
            issues.append({"code": "abnormal_tail_pause", "segment": index,
                           "detail": f"句尾空白 {tail:.2f}s"})

    for index in range(1, len(rows)):
        previous, current = rows[index - 1], rows[index]
        previous_segment, current_segment = segments[index - 1], segments[index]
        pause = previous_segment["tail_silence"] + current_segment["head_silence"]
        overlap = (int(previous.get("src", 1)) == int(current.get("src", 1))
                   and max(float(previous["start"]), float(current["start"]))
                   < min(float(previous["end"]), float(current["end"])) - 0.02)
        item = {"junction": index - 1, "left_segment": index - 1,
                "right_segment": index, "estimated_pause": round(pause, 3),
                "overlap": overlap, "ok": pause <= 0.65 and not overlap}
        junctions.append(item)
        if pause > 0.65:
            issues.append({"code": "abnormal_junction_pause", "segments": [index - 1, index],
                           "detail": f"连接点空白约 {pause:.2f}s"})
        if overlap:
            issues.append({"code": "junction_overlap", "segments": [index - 1, index],
                           "detail": "连接点源时间重叠，可能抢话"})
    return {"ok": not issues, "segments": segments, "junctions": junctions,
            "issue_count": len(issues), "issues": issues,
            "asr_source": "locked_source_word_timestamps"}


def analyze_preview_asr(rows: list[dict], preview_words: list[dict]) -> dict:
    """Audit words decoded from the rendered preview on its playback clock."""
    issues, segments, junctions = [], [], []
    cursor = 0.0
    ranges = []
    for row in rows:
        duration = max(0.0, float(row["end"]) - float(row["start"]))
        ranges.append((cursor, cursor + duration))
        cursor += duration

    assigned_words: list[list[dict]] = [[] for _ in ranges]
    for word in preview_words:
        ws = float(word.get("s", 0))
        we = float(word.get("e", 0))
        best_idx, best_overlap = -1, 0.0
        for idx, (start, end) in enumerate(ranges):
            overlap = max(0.0, min(end, we) - max(start, ws))
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = idx
        if best_idx >= 0:
            assigned_words[best_idx].append(word)
        elif ranges:
            mid = (ws + we) / 2
            closest_idx = min(range(len(ranges)),
                              key=lambda i: abs((ranges[i][0] + ranges[i][1]) / 2 - mid))
            assigned_words[closest_idx].append(word)

    for index, (row, words) in enumerate(zip(rows, assigned_words)):
        actual = "".join(str(word.get("w") or "") for word in words)
        expected = str(row.get("text") or "")
        exp_norm = norm(expected)
        act_norm = norm(actual)
        matcher = difflib.SequenceMatcher(None, exp_norm, act_norm)
        similarity = matcher.ratio() if exp_norm else 1.0
        segment = {"segment": index, "planned_text": expected, "preview_asr": actual,
                   "similarity": round(similarity, 4)}
        segments.append(segment)
        matching_blocks = matcher.get_matching_blocks()
        last_match_end = (matching_blocks[-2].a + matching_blocks[-2].size) if len(matching_blocks) > 1 else 0
        tail_missing = len(exp_norm) - last_match_end
        if similarity < 0.86:
            direction = "漏字/断尾" if len(act_norm) < len(exp_norm) else "多字/串音"
            issues.append({"code": "preview_asr_mismatch", "segment": index,
                           "detail": f"预览二次 ASR 疑似{direction}，相似度 {similarity:.2f}"})
        elif tail_missing >= 2 and len(act_norm) < len(exp_norm):
            issues.append({"code": "preview_asr_tail_truncated", "segment": index,
                           "detail": f"预览二次 ASR 疑似尾音截断/断尾，结尾缺少“{exp_norm[last_match_end:]}”"})
    for index in range(1, len(ranges)):
        boundary = ranges[index][0]
        left = [word for word in preview_words if float(word.get("e", 0)) <= boundary + 0.08]
        right = [word for word in preview_words if float(word.get("s", 0)) >= boundary - 0.08]
        # Whisper word timestamps commonly straddle an edit by a few frames even
        # for clean concatenation.  Only a long token with substantial audio on
        # both sides is evidence of a real overlap/cut-through.
        crossing = [word for word in preview_words
                    if float(word.get("e", 0)) - float(word.get("s", 0)) > 1.0
                    and boundary - float(word.get("s", 0)) > 0.35
                    and float(word.get("e", 0)) - boundary > 0.35]
        gap = (float(right[0]["s"]) - float(left[-1]["e"])) if left and right else 99.0
        item = {"junction": index - 1, "playback_time": round(boundary, 3),
                "recognized_gap": round(gap, 3),
                "crossing_tokens": [str(word.get("w") or "") for word in crossing],
                "ok": gap <= 0.65 and not crossing}
        junctions.append(item)
        if gap > 0.65:
            issues.append({"code": "preview_abnormal_junction_pause",
                           "segments": [index - 1, index],
                           "detail": f"预览连接点识别停顿 {gap:.2f}s"})
        if crossing:
            issues.append({"code": "preview_junction_overlap",
                           "segments": [index - 1, index],
                           "detail": "预览连接点有跨界词，疑似重叠或切词"})
    return {"ok": not issues, "segments": segments, "junctions": junctions,
            "issue_count": len(issues), "issues": issues,
            "asr_source": "rendered_audio_preview"}


def transcribe_preview(path: Path, backend: str = "auto", model: str | None = None
                       ) -> tuple[list[dict], dict]:
    transcriber = Transcriber(backend, model)
    decoded = transcriber.transcribe(str(path), language="zh", word_timestamps=True,
                                     initial_prompt="以下是普通话口播，请使用简体中文。",
                                     beam_size=5)
    words = [word for segment in decoded for word in segment.get("words", [])]
    return words, transcriber.identity


def render_preview(source: Path, rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.1",
            "-c:a", "aac", "-b:a", "32k", str(output),
        ], check=True)
        return
    filters, labels = [], []
    for index, row in enumerate(rows):
        labels.append(f"[a{index}]")
        filters.append(f"[0:a]atrim=start={float(row['start']):.3f}:end={float(row['end']):.3f},"
                       f"asetpts=PTS-STARTPTS[a{index}]")
    filters.append("".join(labels) + f"concat=n={len(rows)}:v=0:a=1[outa]")
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-filter_complex", ";".join(filters), "-map", "[outa]", "-ac", "1", "-ar", "16000",
        "-c:a", "aac", "-b:a", "32k", str(output),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-1200:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("timeline")
    parser.add_argument("source")
    parser.add_argument("word_map")
    parser.add_argument("--preview", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    parser.add_argument("--model")
    args = parser.parse_args()
    rows = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    source_report = analyze_junctions(rows, load_word_map(Path(args.word_map)))
    render_preview(Path(args.source), rows, Path(args.preview))
    try:
        preview_words, identity = transcribe_preview(
            Path(args.preview), args.backend, args.model)
        preview_report = analyze_preview_asr(rows, preview_words)
        preview_report["asr_identity"] = identity
    except Exception as exc:
        preview_report = {"ok": False, "segments": [], "junctions": [],
                          "issue_count": 1, "issues": [{
                              "code": "preview_asr_unavailable",
                              "detail": f"生成预览后无法执行二次 ASR：{exc}"}],
                          "asr_source": "rendered_audio_preview"}
    issues = [*source_report["issues"], *preview_report["issues"]]
    report = {"ok": not issues, "issue_count": len(issues), "issues": issues,
              "source_timestamp_analysis": source_report,
              "preview_asr_analysis": preview_report,
              "segments": preview_report["segments"],
              "junctions": preview_report["junctions"],
              "asr_source": "rendered_audio_preview"}
    report["preview"] = str(Path(args.preview).resolve())
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"audio acceptance: {len(rows)} segments, {len(report['junctions'])} junctions, "
          f"{report['issue_count']} issues -> {args.report}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
