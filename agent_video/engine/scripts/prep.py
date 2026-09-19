# -*- coding: utf-8 -*-
"""预索引：把长素材变成一份落盘的小索引，后续所有步骤只读文件，不再重读转写。

用法:
  python prep.py <media> [--subtitle x.srt] [--workdir DIR] [--model PATH] [--top 150] [--no-words]

产物（全部落在 workdir，默认 <media同目录>/_slice/）：
  probe.txt        素材规格（分辨率/帧率/时长/音轨）
  audio16k.wav     16k 单声道提取音轨（切点与 QA 复用）
  words.json       ASR 词级边界，或字幕块可信起止边界
  sentences.json   完整句 + 时间码（字幕块已合并）
  candidates.json  硬禁词过滤后的完整候选（仅供机器压缩）
  subtitle_blocks.json  原始字幕块（SRT/VTT/ASS/TXT 时间码模式）

设计要点：
  - 违禁词在本地过滤，命中的整句直接淘汰，不进入上下文。
  - 默认只输出数量摘要；候选原文只在 --verbose 时打印。
  - 所有产物落盘，上下文被压缩后可随时回读文件，不必重跑。
"""
import argparse
import atexit
import hashlib
import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows degrades to process-local cache safety
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from badvocab import BAD_RE, review_hits  # noqa: E402
from asr_backend import Transcriber, resolve_config  # noqa: E402
from textnorm import content_rejection, context_dependent_start, incomplete_ending  # noqa: E402

try:
    from zhconv import convert as _zh
except Exception:
    def _zh(t, *a):
        return t

PROMPT_ZH = "以下是普通话口播，请使用简体中文，带标点。"
FINAL_PUNCT = "。！？!?…"


def simp(t):
    """繁简归一：whisper 局部窗口常吐繁体，简体字表会对不上。"""
    return _zh(t or "", "zh-cn")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def sh(cmd):
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{r.stderr[:800]}")
    return r.stdout


def fingerprint(path, sample_size=1024 * 1024):
    """Cheap source identity: metadata plus hashes of the first/last MiB."""
    path = os.path.abspath(path)
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(sample_size))
        if stat.st_size > sample_size:
            handle.seek(max(0, stat.st_size - sample_size))
            digest.update(handle.read(sample_size))
    return {"path": path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "edge_sha256": digest.hexdigest()}


def optional_fingerprint(path):
    return fingerprint(path) if path and os.path.isfile(path) else None


def vocab_identity():
    """候选过滤依赖词表配置；配置变化必须让 candidates 缓存失效。"""
    profile = os.environ.get("DOUYIN_VOCAB_PROFILE")
    return {
        "profile": optional_fingerprint(profile),
        "badvocab": fingerprint(os.path.join(os.path.dirname(__file__), "badvocab.py")),
        "textnorm": fingerprint(os.path.join(os.path.dirname(__file__), "textnorm.py")),
    }


def cache_key(media, subtitle, asr_config, no_words):
    return {"version": 5, "media": fingerprint(media),
            "subtitle": fingerprint(subtitle) if subtitle else None,
            "mode": "subtitle" if subtitle else "whisper", "asr": asr_config,
            "no_words": bool(no_words), "vocab": vocab_identity()}


def cache_complete(workdir, key):
    path = os.path.join(workdir, "cache_manifest.json")
    if not os.path.exists(path):
        return False
    try:
        previous = json.load(open(path, encoding="utf-8"))
    except Exception:
        return False
    required = ["probe.txt", "sentences.json", "candidates.json", "transcript_manifest.json"]
    if key["mode"] == "whisper":
        required.extend(["audio16k.wav", "audio16k.wav.manifest.json"])
    if key["mode"] == "whisper" and not key["no_words"]:
        required.append("words.json")
    if key["mode"] == "subtitle":
        required.extend(["subtitle_blocks.json", "words.json"])
    if previous.get("key") != key or not all(
            os.path.exists(os.path.join(workdir, name)) for name in required):
        return False
    if key["mode"] == "subtitle":
        return True
    try:
        audio_manifest = json.load(open(os.path.join(
            workdir, "audio16k.wav.manifest.json"), encoding="utf-8"))
    except Exception:
        return False
    return audio_manifest.get("key", {}).get("media") == key["media"]


def transcript_cache_complete(workdir, key):
    manifest = os.path.join(workdir, "transcript_manifest.json")
    try:
        previous = json.load(open(manifest, encoding="utf-8"))
    except Exception:
        return False
    required = ["sentences.json"]
    if key["mode"] == "whisper" and not key["no_words"]:
        required.append("words.json")
    if key["mode"] == "subtitle":
        required.extend(["subtitle_blocks.json", "words.json"])
    return previous.get("key") == key and all(
        os.path.isfile(os.path.join(workdir, name)) for name in required)


def probe(media, workdir):
    out = sh(["ffprobe", "-v", "error", "-show_streams", "-show_format",
              "-of", "json", media])
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"].get("duration") or 0)
    fps = 0.0
    if v:
        num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1)
        if fps > 100:            # 1440x2560 等尺寸下 r_frame_rate 会误报，看 avg
            num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
            fps = float(num) / float(den or 1)
    lines = [
        f"文件: {os.path.basename(media)}",
        f"时长: {int(dur // 60)}分{int(dur % 60)}秒 ({dur:.2f}s)",
        f"画面: {v['width']}x{v['height']} @ {fps:.2f}fps" if v else "画面: 无",
        f"音频: {a['codec_name']} {a['sample_rate']}Hz {a['channels']}ch" if a else "音频: 无",
        f"码率: {int(info['format'].get('bit_rate') or 0) // 1000} kbps",
    ]
    txt = "\n".join(lines)
    open(os.path.join(workdir, "probe.txt"), "w", encoding="utf-8").write(txt + "\n")
    return txt, dur


def extract_audio(media, workdir):
    """提取并按源指纹缓存音轨，禁止同一 workdir 复用其他素材的旧 WAV。"""
    wav = os.path.join(workdir, "audio16k.wav")
    manifest = wav + ".manifest.json"
    key = {"version": 1, "media": fingerprint(media), "channels": 1,
           "sample_rate": 16000, "codec": "pcm_s16le"}
    try:
        with open(manifest, encoding="utf-8") as handle:
            cached = json.load(handle)
    except Exception:
        cached = None
    if os.path.exists(wav) and cached and cached.get("key") == key:
        return wav
    tmp = wav + ".tmp.wav"
    sh(["ffmpeg", "-y", "-v", "error", "-i", media, "-vn", "-ac", "1",
        "-ar", "16000", "-c:a", "pcm_s16le", tmp])
    os.replace(tmp, wav)
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump({"key": key}, handle, ensure_ascii=False, indent=2)
    return wav


def dump_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)


def _clock(value):
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        raise ValueError(value)
    return hours * 3600 + minutes * 60 + seconds


def _clean_subtitle_text(value):
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\\[^}]*\}", "", value)
    value = re.sub(r"\\[Nn]", "", value)
    return simp(re.sub(r"\s+", "", value)).strip()


def parse_subtitle(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        raw = handle.read()
    blocks = []
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".ass", ".ssa"}:
        for line in raw.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",", 9)
            if len(fields) < 10:
                continue
            try:
                start, end = _clock(fields[1]), _clock(fields[2])
            except ValueError:
                continue
            body = _clean_subtitle_text(fields[9])
            if body and end > start:
                blocks.append({"start": round(start, 3), "end": round(end, 3), "text": body})
        return blocks
    for chunk in re.split(r"\n\s*\n", raw.strip()):
        lines = [l for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = re.match(r"([^\s]+)\s*-->\s*([^\s]+)", lines[ti].strip())
        if not m:
            continue
        try:
            start, end = _clock(m.group(1)), _clock(m.group(2))
        except ValueError:
            continue
        body = _clean_subtitle_text(" ".join(lines[ti + 1:]))
        if body and end > start:
            blocks.append({"start": round(start, 3), "end": round(end, 3), "text": body})
    if blocks:
        return blocks
    # 兼容常见的 TXT 时码：[00:01.200 --> 00:03.400] 文本
    for line in raw.splitlines():
        match = re.match(r"\s*\[?([^\s\]]+)\s*(?:-->|-|\u81f3)\s*([^\s\]]+)\]?\s*(.*)", line)
        if not match:
            continue
        try:
            start, end = _clock(match.group(1)), _clock(match.group(2))
        except ValueError:
            continue
        body = _clean_subtitle_text(match.group(3))
        if body and end > start:
            blocks.append({"start": round(start, 3), "end": round(end, 3), "text": body})
    return blocks


def words_from_subtitles(blocks):
    """保留字幕块的真实边界，不伪造块内逐字时间。"""
    return [{"s": round(float(block["start"]), 3),
             "e": round(float(block["end"]), 3),
             "w": block["text"], "source": "subtitle_block", "boundary_only": True}
            for block in blocks if str(block.get("text", "")).strip()
            and float(block["end"]) > float(block["start"])]


def merge_blocks(blocks, max_gap=0.55, max_dur=6.5, max_blocks=3,
                 hard_max_dur=10.0, hard_max_blocks=6):
    """一个字幕块不等于一句完整话：按语义/停顿合并到句尾完整为止。
    通常间隔 ≤0.55s、累计 ≤6.5s、最多 3 块；明显承接块可扩到硬上限，
    避免把一句话切成两个无法独立理解的片段。"""
    out = []
    cur = None
    for b in blocks:
        if cur is None:
            cur = dict(b)
            cur["_n"] = 1
            continue
        gap = b["start"] - cur["end"]
        prev_ends = cur["text"][-1] in FINAL_PUNCT
        combined_duration = b["end"] - cur["start"]
        within_soft_limit = cur["_n"] < max_blocks and combined_duration <= max_dur
        # Subtitle blocks are display units, not sentences.  If the next block visibly
        # continues the current phrase (for example "腰部" + "的一个线条"), extend past
        # the ordinary compact-candidate limit instead of manufacturing two fragments.
        semantic_continuation = (
            (incomplete_ending(cur["text"]) is not None
             or context_dependent_start(b["text"]) is not None)
            and cur["_n"] < hard_max_blocks
            and combined_duration <= hard_max_dur
        )
        if gap <= max_gap and not prev_ends and (within_soft_limit or semantic_continuation):
            cur["text"] += b["text"]
            cur["end"] = b["end"]
            cur["_n"] += 1
        else:
            out.append(cur)
            cur = dict(b)
            cur["_n"] = 1
    if cur:
        out.append(cur)
    for x in out:
        x.pop("_n", None)
    return out


def whisper_full(wav, transcriber, want_words=True):
    segs = transcriber.transcribe(wav, language="zh", word_timestamps=want_words,
                                  initial_prompt=PROMPT_ZH, beam_size=5)
    sentences, words = [], []
    for s in segs:
        t = simp((s.get("text") or "").strip())
        if not t:
            continue
        sentences.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "text": t})
        for w in s.get("words", []):
            ch = simp((w.get("w") or "").strip())
            if ch:
                words.append({"s": round(w["s"], 3), "e": round(w["e"], 3), "w": ch})
    return sentences, words


def is_cjk(c):
    return "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf"


def usable(s):
    t = s["text"]
    if len(t) < 5:
        return False
    han = sum(1 for c in t if is_cjk(c))
    if han / max(1, len(t)) < 0.5:
        return False
    if BAD_RE.search(t):
        return False
    if content_rejection(t):
        return False
    if s["end"] - s["start"] < 0.5:
        return False
    return True


def ts(t):
    return f"{int(t // 60)}:{t % 60:05.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--subtitle", "--srt", dest="subtitle")
    ap.add_argument("--workdir")
    ap.add_argument("--model")
    ap.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--no-words", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    media = os.path.abspath(a.media)
    if not os.path.exists(media):
        sys.exit(f"素材不存在: {media}")
    workdir = a.workdir or os.path.join(os.path.dirname(media), "_slice")
    os.makedirs(workdir, exist_ok=True)
    # Multiple edits of the same source may start together and share this index.
    # Keep the deterministic cache writer single-process; the lock is released
    # automatically when the short-lived prep process exits.
    cache_lock_handle = open(os.path.join(workdir, ".prep.lock"), "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(cache_lock_handle.fileno(), fcntl.LOCK_EX)

        asr_config = None if a.subtitle else resolve_config(a.backend, a.model)
        subtitle = os.path.abspath(a.subtitle) if a.subtitle else None
        key = cache_key(media, subtitle, asr_config, a.no_words)
        if not a.force and cache_complete(workdir, key):
            print(f"cache hit -> {workdir}")
            return

        t0 = time.time()
        ptxt, dur = probe(media, workdir)
        print(ptxt)
        # Time-coded subtitles are sufficient for candidate selection and normal
        # boundary alignment.  Defer audio extraction unless cuts.py actually needs
        # an ASR/silence fallback after picks are known.
        wav = (os.path.join(workdir, "audio16k.wav") if subtitle
               else extract_audio(media, workdir))

        transcript_key = {"version": 3, "media": fingerprint(media),
                          "subtitle": fingerprint(subtitle) if subtitle else None,
                          "mode": "subtitle" if subtitle else "whisper", "asr": asr_config,
                          "no_words": bool(a.no_words)}
        words = []
        if transcript_cache_complete(workdir, transcript_key):
            sentences = json.load(open(os.path.join(workdir, "sentences.json"), encoding="utf-8"))
            words_path = os.path.join(workdir, "words.json")
            if os.path.isfile(words_path) and not a.no_words:
                words = json.load(open(words_path, encoding="utf-8"))
            mode = f"转写缓存（{len(sentences)} 句）"
        else:
            if subtitle:
                blocks = parse_subtitle(subtitle)
                if not blocks:
                    raise ValueError(f"字幕文件没有可用时间码: {subtitle}")
                dump_json(os.path.join(workdir, "subtitle_blocks.json"), blocks)
                sentences = merge_blocks(blocks)
                words = words_from_subtitles(blocks)
                if words:
                    dump_json(os.path.join(workdir, "words.json"), words)
                mode = f"字幕直用模式（{len(blocks)} 个可信块边界，0 次 ASR）"
            else:
                transcriber = Transcriber(a.backend, a.model)
                sentences, words = whisper_full(wav, transcriber, want_words=not a.no_words)
                mode = f"Whisper {transcriber.backend} 模式（{len(sentences)} 句）"
                if words:
                    dump_json(os.path.join(workdir, "words.json"), words)
            dump_json(os.path.join(workdir, "sentences.json"), sentences)
            with open(os.path.join(workdir, "transcript_manifest.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"key": transcript_key}, handle, ensure_ascii=False, indent=2)

        kept = []
        for sentence in sentences:
            if usable(sentence):
                item = dict(sentence)
                review = review_hits(item.get("text", ""))
                if review:
                    item["review_terms"] = review
                kept.append(item)
        dropped = len(sentences) - len(kept)
        dump_json(os.path.join(workdir, "candidates.json"), kept)
        with open(os.path.join(workdir, "cache_manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"key": key, "created_at": int(time.time())}, handle,
                      ensure_ascii=False, indent=2)

        print(f"\n索引完成 → {workdir}")
        print(f"  {mode}")
        unit = "字幕块" if subtitle else "词"
        audio_state = ("audio16k.wav 延迟提取（正常字幕边界路径无需音频）"
                       if subtitle and not os.path.isfile(wav) else "audio16k.wav 可用")
        print(f"  {audio_state} / sentences.json {len(sentences)} 句"
              + (f" / words.json {len(words)} 个{unit}" if words else " / 无 words.json"))
        print(f"  candidates.json {len(kept)} 句（本地过滤剔除 {dropped} 句，含违禁词/过短/非中文）")
        print(f"  耗时 {time.time() - t0:.0f}s")
        if a.verbose:
            print(f"\n候选完整句（仅 verbose，显示前 {a.top} 句）：")
            for i, s in enumerate(kept[:a.top]):
                print(f"{i:4d} {ts(s['start'])}-{ts(s['end'])} "
                      f"({s['end'] - s['start']:4.1f}s)  {s['text']}")
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(cache_lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        cache_lock_handle.close()


if __name__ == "__main__":
    main()
