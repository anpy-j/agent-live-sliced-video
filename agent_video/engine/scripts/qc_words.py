# -*- coding: utf-8 -*-
"""Write rendered-video word timestamps to JSON without printing raw words."""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr_backend import Transcriber  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--audio")
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    parser.add_argument("--out")
    args = parser.parse_args()
    video = os.path.abspath(args.video)
    generated_audio = args.audio is None
    wav = args.audio or os.path.join(os.path.dirname(video), "_render16k.wav")
    output = args.out or os.path.join(os.path.dirname(video), "qc_words.json")
    if generated_audio or not os.path.exists(wav):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)

    model = Transcriber(args.backend, args.model)
    segments = model.transcribe(wav, language="zh", word_timestamps=True, beam_size=5)
    words, segment_rows = [], []
    for segment in segments:
        segment_words = []
        for word in segment.get("words", []):
            text = (word.get("w") or "").strip()
            if not text:
                continue
            row = {"s": round(float(word["s"]), 3), "e": round(float(word["e"]), 3), "w": text}
            words.append(row)
            segment_words.append(row)
        segment_rows.append({"s": round(float(segment["start"]), 3),
                             "e": round(float(segment["end"]), 3),
                             "text": (segment.get("text") or "").strip(),
                             "words": segment_words})
    with open(output, "w", encoding="utf-8") as handle:
        json.dump({"video": video, "asr": model.identity, "word_count": len(words),
                   "words": words, "segments": segment_rows},
                  handle, ensure_ascii=False, indent=1)
    if generated_audio and os.path.exists(wav):
        os.remove(wav)
    print(f"render word timestamps: {len(words)} words -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
