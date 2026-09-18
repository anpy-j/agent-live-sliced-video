# -*- coding: utf-8 -*-
"""Create a bounded whole-source overview; selected-segment frame extraction is unsupported."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont


CELL_WIDTH = 236


def font(size=13, path=None):
    candidate = path or os.environ.get("LIVECUT_OVERVIEW_FONT")
    if candidate:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception as exc:
            raise SystemExit(f"无法加载素材概览字体: {candidate}: {exc}")
    return ImageFont.load_default()


def grab(media, timestamp, output):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", media,
                    "-frames:v", "1", "-q:v", "3", output], check=True)


def timestamp_label(value):
    return f"{int(value // 60)}:{value % 60:04.1f}"


def fingerprint(path):
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(1024 * 1024))
    return {"path": os.path.abspath(path), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "head_sha256": digest.hexdigest()}


def build_overview(items, output, font_path=None):
    if not items:
        raise SystemExit("没有可生成概览的帧")
    first = Image.open(items[0][1])
    cell_height = int(CELL_WIDTH * first.height / first.width)
    columns = 6
    rows = (len(items) + columns - 1) // columns
    canvas = Image.new("RGB", (columns * CELL_WIDTH, rows * (cell_height + 20)),
                       (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    label_font = font(13, font_path)
    for index, (label, path) in enumerate(items):
        image = Image.open(path).convert("RGB").resize((CELL_WIDTH, cell_height))
        x = (index % columns) * CELL_WIDTH
        y = (index // columns) * (cell_height + 20)
        canvas.paste(image, (x, y))
        draw.text((x + 6, y + cell_height + 3), label, fill=(0, 0, 0), font=label_font)
    canvas.save(output, quality=85)
    return canvas.size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["overview"])
    parser.add_argument("input")
    parser.add_argument("out")
    parser.add_argument("--n", type=int, default=24)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--font")
    args = parser.parse_args()
    if not 1 <= args.n <= 30:
        parser.error("overview --n must be between 1 and 30")

    manifest_path = args.out + ".manifest.json"
    key = {"version": 3, "mode": "overview", "input": fingerprint(args.input),
           "n": args.n, "columns": 6, "font": args.font}
    if not args.force and os.path.exists(args.out) and os.path.exists(manifest_path):
        if json.load(open(manifest_path, encoding="utf-8")).get("key") == key:
            print(f"素材概览缓存命中 → {args.out}")
            return

    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", args.input], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode:
        raise RuntimeError(probe.stderr[-1000:])
    duration = float(probe.stdout.strip())
    temporary = tempfile.mkdtemp(prefix="livecut-overview-")
    try:
        step = duration / args.n
        items = []
        for index in range(args.n):
            at = index * step
            path = os.path.join(temporary, f"frame-{index}.jpg")
            grab(args.input, at, path)
            items.append((f"{index:02d}  {timestamp_label(at)}", path))
        size = build_overview(items, args.out, args.font)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "outputs": [os.path.abspath(args.out)]}, handle,
                      ensure_ascii=False, indent=2)
        print(f"素材概览：{len(items)} 帧（预算上限 30）→ {args.out} {size}")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    main()
