# -*- coding: utf-8 -*-
"""SRT 模式下补一份全片逐字时间戳（`words.json`）。

用法:
    python words.py <audio16k.wav> <out.json> [--backend auto] [--model PATH]

为什么需要这一步：`prep.py --srt` 走 SRT 优先模式，跳过全片语音识别，因此**不产出
`words.json`**；而 `cuts.py --words` 和渲染前必跑的 `audit_bounds.py` 都依赖全片逐字
时间戳。缺了它，`cuts.py` 只能对每个入选段做局部补转写 —— 慢、窗口外的邻居字拿不到、
边界审计（首尾静音、切半字、「带料」）直接不可用，而且局部窗口会稳定吐出幽灵文本
（实测 60 个窗口里 15 个吐「请不吝点赞订阅…」，全片单次转写 5511 个字条里一个都没有）。

所以 SRT 模式下必须在定切点之前跑一次本脚本。`run_slice.py --srt` 会自动调用它
（`words.json` 已存在则跳过，缓存有效时不重复转写）。

产物格式与 `prep.py` 的 whisper 模式完全一致：`[{"s":…, "e":…, "w":…}, …]`，
`cuts.py` 的 `expand()` 会自行按字数均分并补 token 结束时间与拼音。
耗时参考：6 分 52 秒素材约 60~100 秒（CPU int8）。
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prep  # noqa: E402
from asr_backend import Transcriber  # noqa: E402


def build_words(wav, backend=None, model=None):
    """全片转写一次，只取逐字条目（与 prep.whisper_full 共用同一套归一化）。"""
    transcriber = Transcriber(backend, model)
    sentences, words = prep.whisper_full(wav, transcriber, want_words=True)
    return transcriber, sentences, words


def file_identity(path):
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return {"path": os.path.abspath(path), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "edge_sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="prep.py 产出的 audio16k.wav")
    parser.add_argument("out", nargs="?", help="输出路径，默认与音频同目录的 words.json")
    parser.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    parser.add_argument("--model")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    wav = os.path.abspath(args.audio)
    if not os.path.isfile(wav):
        raise SystemExit(f"音频不存在: {wav}")
    out = os.path.abspath(args.out or os.path.join(os.path.dirname(wav), "words.json"))
    manifest = out + ".manifest.json"
    with open(prep.__file__, "rb") as handle:
        prep_sha256 = hashlib.sha256(handle.read()).hexdigest()
    key = {"version": 1, "audio": file_identity(wav),
           "asr": prep.resolve_config(args.backend, args.model),
           "prep_sha256": prep_sha256}
    try:
        with open(manifest, encoding="utf-8") as handle:
            cached = json.load(handle)
    except Exception:
        cached = None
    if os.path.isfile(out) and not args.force and cached and cached.get("key") == key:
        print(f"words.json 已存在，跳过: {out}")
        return 0

    transcriber, sentences, words = build_words(wav, args.backend, args.model)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(words, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump({"key": key}, handle, ensure_ascii=False, indent=2)
    if not words:
        # 空词表不是正常结果：要么素材确实是纯 BGM/静音，要么 ASR 没跑起来。
        # 说清楚，否则只会看到 cuts 全线退回 relaxed + audit 报 need_manual。
        print("!! 全片没有取到任何逐字条目：素材可能是纯音乐/静音，或 ASR 失败；"
              "cuts.py 会退回逐窗口补转写，边界审计将不可用")
    print(f"全片逐字转写: {transcriber.backend} / {len(sentences)} 句 / "
          f"{len(words)} 个 token -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
