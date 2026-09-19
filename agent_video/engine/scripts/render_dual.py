# -*- coding: utf-8 -*-
"""Render a dual timeline: selected speech audio plus independent visual clips.

竖屏源保留原分辨率并封顶 1440x2560、源帧率、AAC 256k、响度目标
-6.5 LUFS。编码器与速度参数由上层生产配置显式传入。
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffmpeg_graph import filter_complex_args  # noqa: E402


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result.stdout


def parse_sources(values):
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.isdigit() or not os.path.isfile(path):
            raise SystemExit(f"Invalid --src: {value!r}; expected ID=existing-media")
        result[int(key)] = os.path.abspath(path)
    return result


def source_fps(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", path]).strip()
    num, _, den = out.partition("/")
    return float(num) / float(den or 1)


def source_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path]).strip()
    width, height = (int(value) for value in out.split("x"))
    return width, height


def output_size(path, width, height):
    if (width is None) != (height is None):
        raise SystemExit("Provide both --width and --height, or neither")
    if width is not None:
        return width, height
    src_w, src_h = source_size(path)
    if src_h >= src_w:
        # 与 render_multi.py 保持同一封顶值（账号成片标准 1440x2560）。
        ratio = min(1.0, 1440 / src_w, 2560 / src_h)
        return max(2, int(src_w * ratio) // 2 * 2), max(2, int(src_h * ratio) // 2 * 2)
    return 1440, 2560


def video_filter(width, height, fps, allow_upscale, crop_x=0.5):
    crop_x = min(1.0, max(0.0, float(crop_x)))
    # Wider-than-target inputs are reframed to portrait around the content-aware center.
    target_ratio = width / height
    smart_crop = (f"crop=w='if(gt(iw/ih\\,{target_ratio:.9f})\\,ih*{target_ratio:.9f}\\,iw)':"
                  f"h='if(gt(iw/ih\\,{target_ratio:.9f})\\,ih\\,ih)':"
                  f"x='if(gt(iw/ih\\,{target_ratio:.9f})\\,(iw-ow)*{crop_x:.6f}\\,0)':y=0,")
    if allow_upscale:
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
    else:
        ratio = f"min(1\\,min({width}/iw\\,{height}/ih))"
        scale = (f"scale=w='trunc(iw*{ratio}/2)*2':"
                 f"h='trunc(ih*{ratio}/2)*2',")
    return (smart_crop + scale + f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps:.6f},setsar=1,setpts=PTS-STARTPTS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("timeline")
    parser.add_argument("output")
    parser.add_argument("--src", action="append", default=[])
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--crf", type=int, default=16)
    parser.add_argument("--preset", default="slow")
    parser.add_argument("--video-codec", choices=["libx264", "h264_videotoolbox"],
                        default="libx264")
    parser.add_argument("--video-bitrate", default="18M")
    parser.add_argument("--audio-bitrate", default="256k")
    parser.add_argument("--no-loudnorm", action="store_true")
    parser.add_argument("--loudness", type=float, default=-6.5)
    parser.add_argument("--audio-edge-ms", type=float, default=12.0)
    parser.add_argument("--allow-upscale", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if os.path.exists(args.output) and not args.force:
        raise SystemExit(f"Output exists; use --force to replace: {args.output}")
    with open(args.timeline, encoding="utf-8") as handle:
        rows = json.load(handle)
    sources = parse_sources(args.src)
    if not rows:
        raise SystemExit("Dual timeline is empty")
    first_src = int((rows[0].get("video") or [rows[0].get("audio")])[0]["src"])
    fps = args.fps or source_fps(sources[first_src])
    args.width, args.height = output_size(sources[first_src], args.width, args.height)

    command, audio_inputs, video_inputs = ["ffmpeg", "-y", "-v", "error"], [], []
    input_index = 0
    for block_index, block in enumerate(rows):
        audio = block.get("audio") or {}
        audio_src = int(audio.get("src", -1))
        if audio_src not in sources:
            raise SystemExit(f"Block {block_index}: missing audio source {audio_src}")
        audio_start, audio_end = float(audio["start"]), float(audio["end"])
        audio_duration = audio_end - audio_start
        if audio_duration <= 0:
            raise SystemExit(f"Block {block_index}: invalid audio duration")
        command += ["-ss", f"{audio_start:.6f}", "-t", f"{audio_duration:.6f}",
                    "-i", sources[audio_src]]
        audio_inputs.append((input_index, block_index))
        input_index += 1

        remaining = audio_duration
        for piece_index, piece in enumerate(block.get("video") or []):
            if remaining <= 0.001:
                break
            video_src = int(piece.get("src", -1))
            if video_src not in sources:
                raise SystemExit(f"Block {block_index}: missing video source {video_src}")
            start, end = float(piece["start"]), float(piece["end"])
            use_duration = min(end - start, remaining)
            if use_duration <= 0:
                raise SystemExit(f"Block {block_index} piece {piece_index}: invalid duration")
            command += ["-ss", f"{start:.6f}", "-t", f"{use_duration:.6f}",
                        "-i", sources[video_src]]
            # Keep render metadata with the input.  Looking up `piece` later would
            # reuse the final loop value and apply one crop position to every clip.
            video_inputs.append((input_index, block_index, piece_index,
                                 float(piece.get("crop_x", 0.5))))
            input_index += 1
            remaining -= use_duration
        if remaining > 0.001:
            raise SystemExit(f"Block {block_index}: video is {remaining:.3f}s shorter than audio")

    filters, audio_labels, video_labels = [], [], []
    for input_id, block_index in audio_inputs:
        label = f"a{block_index}"
        audio = rows[block_index]["audio"]
        duration = float(audio["end"]) - float(audio["start"])
        edge = max(0.0, min(args.audio_edge_ms / 1000.0, duration / 4.0))
        fades = (f",afade=t=in:st=0:d={edge:.4f},"
                 f"afade=t=out:st={max(0.0, duration - edge):.4f}:d={edge:.4f}") if edge else ""
        filters.append(f"[{input_id}:a]aresample=async=1:first_pts=0,"
                       f"aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"asetpts=PTS-STARTPTS{fades}[{label}]")
        audio_labels.append(f"[{label}]")
    for input_id, block_index, piece_index, crop_x in video_inputs:
        label = f"v{block_index}_{piece_index}"
        filters.append(f"[{input_id}:v]"
                       f"{video_filter(args.width, args.height, fps, args.allow_upscale, crop_x)}[{label}]")
        video_labels.append(f"[{label}]")
    filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vcat]")
    filters.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[acat]")
    audio_filter = "anull" if args.no_loudnorm else \
        f"loudnorm=I={args.loudness}:TP=-1.0:LRA=8,alimiter=limit=0.85:level=disabled"
    filters.append(f"[acat]{audio_filter}[aout]")
    final_output = os.path.abspath(args.output)
    partial_output = final_output + ".partial.mp4"
    video_encoding = (["-c:v", "h264_videotoolbox", "-b:v", args.video_bitrate,
                       "-realtime", "true"] if args.video_codec == "h264_videotoolbox"
                      else ["-c:v", "libx264", "-crf", str(args.crf),
                            "-preset", args.preset])
    filter_args, filter_script = filter_complex_args(filters)
    command += [*filter_args, "-map", "[vcat]", "-map", "[aout]",
                *video_encoding,
                "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}", "-c:a", "aac",
                "-b:a", args.audio_bitrate, "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", "-shortest", partial_output]
    try:
        run(command)
    finally:
        if os.path.exists(filter_script):
            os.unlink(filter_script)
    os.replace(partial_output, final_output)
    print(f"dual render: {len(audio_inputs)} audio blocks, {len(video_inputs)} visual clips "
          f"-> {args.output}")


if __name__ == "__main__":
    main()
