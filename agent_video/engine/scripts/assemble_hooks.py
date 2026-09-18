# -*- coding: utf-8 -*-
"""Create hook/body seam previews and a compact, unranked hook manifest.

Usage:
  python assemble_hooks.py body.mp4 OUTDIR --hook A=hook_a.mp4 --hook B=hook_b.mp4
"""
import argparse
import json
import os
import subprocess


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", path]).strip())


def stream_signature(path):
    raw = run(["ffprobe", "-v", "error", "-show_entries",
               "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,sample_rate,channels,channel_layout",
               "-of", "json", path])
    return json.loads(raw).get("streams", [])


def parse_hooks(values):
    hooks = []
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not os.path.isfile(path):
            raise SystemExit(f"Invalid --hook: {value!r}; expected NAME=existing-video")
        hooks.append((name, os.path.abspath(path)))
    if not hooks:
        raise SystemExit("At least one hook is required")
    return hooks


def make_preview(hook, body, output, body_seconds):
    graph = (
        f"[0:v]setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS[a0];"
        f"[1:v]trim=duration={body_seconds:.3f},setpts=PTS-STARTPTS[v1];"
        f"[1:a]atrim=duration={body_seconds:.3f},asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", hook, "-i", body,
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", output])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("body")
    parser.add_argument("outdir")
    parser.add_argument("--hook", action="append", default=[])
    parser.add_argument("--body-preview-seconds", type=float, default=10.0)
    parser.add_argument("--text-map", help="Optional JSON object: hook name -> transcript")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not os.path.isfile(args.body):
        raise SystemExit(f"Body does not exist: {args.body}")
    hooks = parse_hooks(args.hook)
    os.makedirs(args.outdir, exist_ok=True)
    text_map = json.load(open(args.text_map, encoding="utf-8")) if args.text_map else {}
    body_sig = stream_signature(args.body)
    body_duration = duration(args.body)
    manifest = {"ranked": False, "body": os.path.abspath(args.body),
                "body_duration": round(body_duration, 3), "hooks": []}
    for name, hook in hooks:
        preview = os.path.join(args.outdir, f"preview_{name}+body.mp4")
        if os.path.exists(preview) and not args.force:
            raise SystemExit(f"Preview exists; use --force to replace: {preview}")
        make_preview(hook, args.body, preview, args.body_preview_seconds)
        hook_duration = duration(hook)
        manifest["hooks"].append({
            "name": name,
            "file": hook,
            "preview": os.path.abspath(preview),
            "text": text_map.get(name, ""),
            "duration": round(hook_duration, 3),
            "combined_duration": round(hook_duration + body_duration, 3),
            "streams_match_body": stream_signature(hook) == body_sig,
        })
    manifest_path = os.path.join(args.outdir, "hook_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    mismatch = sum(not row["streams_match_body"] for row in manifest["hooks"])
    print(f"hook previews: {len(hooks)}, stream mismatches: {mismatch} -> {manifest_path}")


if __name__ == "__main__":
    main()
