# -*- coding: utf-8 -*-
"""Final technical QC for the one-pass HD deliverable."""
import argparse
import json
import os
import re
import subprocess
import sys


LUFS_RANGE = (-9.5, -5.5)
EXPECTED_WIDTH = 1440
EXPECTED_HEIGHT = 2560


def run(cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if check and result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stdout + result.stderr


def fingerprint(path):
    stat = os.stat(path)
    return {"path": os.path.abspath(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def first_number(text, pattern):
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


def detector_issues(log):
    issues = []
    for match in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", log):
        start, end, duration = map(float, match.groups())
        issues.append({"code": "black_video", "start": round(start, 3),
                       "end": round(end, 3), "duration": round(duration, 3)})
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [(float(a), float(b)) for a, b in
            re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", log)]
    for index, (end, duration) in enumerate(ends):
        start = starts[index] if index < len(starts) else end - duration
        issues.append({"code": "long_silence", "start": round(start, 3),
                       "end": round(end, 3), "duration": round(duration, 3)})
    freeze_starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    freeze_ends = [(float(a), float(b)) for a, b in
                   re.findall(r"freeze_end: ([\d.]+) \| freeze_duration: ([\d.]+)", log)]
    for index, (end, duration) in enumerate(freeze_ends):
        start = freeze_starts[index] if index < len(freeze_starts) else end - duration
        issues.append({"code": "frozen_video", "start": round(start, 3),
                       "end": round(end, 3), "duration": round(duration, 3)})
    return issues


def stream_issues(info, expected_width=EXPECTED_WIDTH, expected_height=EXPECTED_HEIGHT):
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    subtitles = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    duration = float(info.get("format", {}).get("duration") or 0)
    issues = []
    if not video:
        issues.append({"code": "missing_video_stream"})
    if not audio:
        issues.append({"code": "missing_audio_stream"})
    if subtitles:
        issues.append({"code": "unexpected_subtitle_stream", "count": len(subtitles)})
    if video and video.get("codec_name") != "h264":
        issues.append({"code": "unexpected_video_codec", "codec": video.get("codec_name")})
    if audio and audio.get("codec_name") != "aac":
        issues.append({"code": "unexpected_audio_codec", "codec": audio.get("codec_name")})
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if (width, height) != (expected_width, expected_height):
            issues.append({"code": "unexpected_resolution", "width": width, "height": height,
                           "expected": [expected_width, expected_height]})
    if video and audio:
        video_duration = float(video.get("duration") or duration)
        audio_duration = float(audio.get("duration") or duration)
        if abs(video_duration - audio_duration) > 0.12:
            issues.append({"code": "av_duration_mismatch", "video": round(video_duration, 3),
                           "audio": round(audio_duration, 3)})
    rotation = (video or {}).get("tags", {}).get("rotate")
    if rotation and str(rotation) not in {"0", "360"}:
        issues.append({"code": "rotation_metadata", "rotate": rotation})
    return video, audio, duration, issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("outdir")
    parser.add_argument("--expected-width", type=int, default=EXPECTED_WIDTH)
    parser.add_argument("--expected-height", type=int, default=EXPECTED_HEIGHT)
    parser.add_argument("--technical-only", action="store_true",
                        help="Compatibility flag; QC is always technical-only")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    report_path = os.path.join(args.outdir, "qc_report.json")
    specs_path = os.path.join(args.outdir, "qc_specs.txt")
    manifest_path = os.path.join(args.outdir, "qc_manifest.json")
    key = {"version": 5, "video": fingerprint(args.video),
           "expected": [args.expected_width, args.expected_height],
           "qc_script": fingerprint(__file__)}
    if not args.force and os.path.exists(manifest_path):
        old = json.load(open(manifest_path, encoding="utf-8"))
        if old.get("key") == key and all(os.path.exists(path) for path in [report_path, specs_path]):
            print(f"qc cache hit -> {args.outdir}")
            return 0 if json.load(open(report_path, encoding="utf-8")).get("ok") else 1

    info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", args.video]))
    video, audio, duration, issues = stream_issues(
        info, args.expected_width, args.expected_height)
    if video and audio:
        detect_log = run([
            "ffmpeg", "-v", "info", "-i", args.video,
            "-filter_complex",
            "[0:v]blackdetect=d=0.25:pix_th=0.10,freezedetect=n=-50dB:d=3[v];"
            "[0:a]silencedetect=n=-42dB:d=1,loudnorm=I=-6.5:TP=-1:LRA=8:print_format=summary[a]",
            "-map", "[a]", "-map", "[v]", "-f", "null", "-"
        ])
        issues.extend(detector_issues(detect_log))
        measured_lufs = first_number(detect_log, r"Input Integrated:\s*([-\d.]+)")
        true_peak = first_number(detect_log, r"Input True Peak:\s*([-\d.]+)")
        if measured_lufs is not None and not LUFS_RANGE[0] <= measured_lufs <= LUFS_RANGE[1]:
            issues.append({"code": "loudness_out_of_target", "lufs": measured_lufs,
                           "target_range": list(LUFS_RANGE)})
        if true_peak is not None and true_peak > -0.5:
            issues.append({"code": "true_peak_too_high", "dbtp": true_peak})
    else:
        measured_lufs = true_peak = None

    specs = [f"file={os.path.basename(args.video)}", f"duration={duration:.3f}",
             f"video={video.get('codec_name')} {video.get('width')}x{video.get('height')}"
             if video else "video=missing",
             f"audio={audio.get('codec_name')} {audio.get('sample_rate')}Hz {audio.get('channels')}ch"
             if audio else "audio=missing",
             f"expected_resolution={args.expected_width}x{args.expected_height}",
             f"integrated_lufs={measured_lufs}", f"true_peak_dbtp={true_peak}"]
    with open(specs_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(specs) + "\n")

    report = {"ok": not issues, "duration": round(duration, 3),
              "issue_count": len(issues), "issues": issues, "errors": issues, "warnings": []}
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"key": key}, handle, ensure_ascii=False, indent=2)
    print(f"qc {'passed' if not issues else 'failed'}: {len(issues)} errors -> {report_path}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
