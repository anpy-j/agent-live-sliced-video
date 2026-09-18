# -*- coding: utf-8 -*-
"""Concatenate source audio into one 16 kHz mono WAV and write source offsets.

Usage:
  python concat_audio.py OUT.wav SOURCES.json --src 1=a.mp4 --src 2=b.mp4
"""
import argparse
import json
import os
import subprocess


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def parse_src(values):
    sources = []
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.isdigit() or not os.path.isfile(path):
            raise SystemExit(f"Invalid --src value: {value!r}; expected ID=existing-media")
        sources.append((int(key), os.path.abspath(path)))
    if not sources:
        raise SystemExit("At least one --src ID=MEDIA is required")
    if len({x[0] for x in sources}) != len(sources):
        raise SystemExit("Duplicate source id")
    return sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("manifest")
    parser.add_argument("--src", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    sources = parse_src(args.src)
    existing = [path for path in (args.output, args.manifest) if os.path.exists(path)]
    if existing and not args.force:
        raise SystemExit(f"Output exists; use --force to replace: {existing[0]}")
    source_durations = [duration(path) for _, path in sources]

    command = ["ffmpeg", "-y", "-v", "error"]
    for _, path in sources:
        command += ["-i", path]
    inputs = "".join(
        f"[{i}:a]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono,"
        f"asetpts=PTS-STARTPTS,atrim=duration={source_durations[i]:.6f},"
        f"apad=whole_dur={source_durations[i]:.6f}[a{i}];"
        for i in range(len(sources)))
    labels = "".join(f"[a{i}]" for i in range(len(sources)))
    graph = inputs + f"{labels}concat=n={len(sources)}:v=0:a=1[outa]"
    command += ["-filter_complex", graph, "-map", "[outa]", "-c:a", "pcm_s16le",
                os.path.abspath(args.output)]
    run(command)

    offset = 0.0
    manifest = {"audio": os.path.abspath(args.output), "sources": {}}
    for (source_id, path), source_duration in zip(sources, source_durations):
        manifest["sources"][str(source_id)] = {
            "media": path,
            "offset": round(offset, 6),
            "duration": round(source_duration, 6),
        }
        offset += source_duration
    manifest["duration"] = round(offset, 6)
    with open(args.manifest, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(f"audio concat: {len(sources)} sources, {offset:.2f}s -> {args.output}")


if __name__ == "__main__":
    main()
