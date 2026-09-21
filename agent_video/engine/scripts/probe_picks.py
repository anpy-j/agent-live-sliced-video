# -*- coding: utf-8 -*-
"""批量探测候选段：一次对齐 + 一次审计，直接给出每段的真实口播与可用性。

为什么需要它：cuts.py 的逐字时间戳是对「选中窗口」做局部转写得到的，同一句话
换个窗口边界就可能转出不同文本，个别区间还会出现空词或幻听。逐轮盲改 picks
再全量重跑是最慢的走法（实测 5 轮、每轮约 142 秒）。
本脚本把一批候选段一次性提交给 cuts.py + audit_bounds.py，共用同一份逐窗口
缓存，然后从结果里读出「真实口播 / 词表覆盖 / 审计问题」，一次挑出干净可用的段。

用法:
  python probe_picks.py <media> <candidates.json> <report.json> [--workdir DIR]

candidates.json:
  [{"start": 100.0, "end": 103.0, "role": "proof", "text": "计划文案"}, ...]

产物 report.json:
  {"ok": bool, "clean_segments": [索引...], "skipped": [...],
   "segments": [{"index","start","end","dur","mode","need_manual",
                 "planned","actual","similarity","word_coverage","issues"}]}

clean_segments 里的段审计零问题且词表覆盖达标，可直接写进 picks.json。
"""
import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from textnorm import cn_num  # noqa: E402


def run(command):
    return subprocess.run([str(x) for x in command], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def norm(text):
    return "".join(c.lower() for c in cn_num(text)
                   if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("candidates")
    ap.add_argument("report")
    ap.add_argument("--workdir")
    ap.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    ap.add_argument("--model")
    args = ap.parse_args()

    media = Path(args.media).resolve()
    workdir = Path(args.workdir).resolve() if args.workdir else media.parent / "_slice"
    index = workdir / "index"
    wav, sentences = index / "audio16k.wav", index / "sentences.json"
    if not wav.is_file() or not sentences.is_file():
        raise SystemExit(f"probe: 索引缺失，请先用 run_slice.py 跑完 prepare：{index}")

    raw = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    items = (raw.get("picks") if isinstance(raw, dict) else raw) or []
    if not items:
        raise SystemExit("probe: candidates 为空")
    sentence_rows = json.loads(sentences.read_text(encoding="utf-8"))

    def script_for(start, end):
        """候选没写文案时，用索引里与它重叠的句级转写补上。

        补文案不是为了好看：cuts.py 靠文案做对齐（拼音 LCS），空文案或占位符
        会让每一段都退化成 relaxed，探测结果全是噪声 —— 只给时间范围时实测
        12 段 0 段可用，看起来像「素材全废」，其实是探针自己没给对齐依据。
        """
        return "".join(q["text"] for q in sentence_rows
                       if q["end"] > start + 1e-6 and q["start"] < end - 1e-6)

    picks = []
    for c in items:
        start, end = float(c["start"]), float(c["end"])
        picks.append({"src": 1, "start": start, "end": end,
                      "role": c.get("role") or "proof", "module": "body",
                      "atom_id": c.get("atom_id"),
                      "required_atom_ids": c.get("required_atom_ids"),
                      "long_complete_utterance": bool(c.get("long_complete_utterance")),
                      "text": (c.get("text") or "").strip() or script_for(start, end)})

    scratch = workdir / "_probe"
    scratch.mkdir(parents=True, exist_ok=True)
    probe_picks = scratch / "picks.json"
    probe_picks.write_text(json.dumps({"offsets": {"1": [0.0, str(media)]}, "picks": picks},
                                      ensure_ascii=False), encoding="utf-8")

    timeline, words_dir = scratch / "timeline.json", scratch / "words"
    command = [sys.executable, HERE / "cuts.py", wav, probe_picks, timeline,
               "--sentences", sentences, "--backend", args.backend,
               "--selected-words-dir", words_dir,
               "--cache", workdir / "align_cache.json"]
    if (index / "words.json").is_file():
        command += ["--words", index / "words.json"]
    if args.model:
        command += ["--model", args.model]
    result = run(command)
    if result.returncode:
        raise SystemExit(f"probe: cuts 失败\n{(result.stdout + result.stderr)[-2000:]}")

    audit_path = scratch / "audit.json"
    run([sys.executable, HERE / "audit_bounds.py", timeline,
         "--word-map-json", words_dir / "word_map.json", "--allow-manual",
         "--report", audit_path])

    rows = json.loads(timeline.read_text(encoding="utf-8"))
    issues = {}
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        for item in audit.get("issues", []):
            issues.setdefault(int(item.get("segment", -1)), []).append(item["type"])
    except Exception:
        pass

    skipped = []
    fix = scratch / "manual_fix.json"
    if fix.is_file():
        skipped = json.loads(fix.read_text(encoding="utf-8")).get("skipped") or []

    manifest = json.loads((words_dir / "word_map.json").read_text(encoding="utf-8"))
    first = next((k for k in manifest if not str(k).startswith("_")), None)
    tokens = json.loads(Path(manifest[first]).read_text(encoding="utf-8")) if first else []

    def skip_reason(pick):
        for item in skipped:
            if abs(float(item["start"]) - pick["start"]) < 0.02:
                return item.get("why")
        return None

    segments, cursor = [], 0
    for pick in picks:
        reason = skip_reason(pick)
        if reason is not None:
            segments.append({"index": None, "start": pick["start"], "end": pick["end"],
                             "dur": round(pick["end"] - pick["start"], 3), "mode": "skipped",
                             "need_manual": None, "planned": pick["text"], "actual": "",
                             "similarity": 0.0, "word_coverage": 0.0,
                             "issues": ["cuts_skipped"], "why": reason})
            continue
        if cursor >= len(rows):
            break
        row_index, row = cursor, rows[cursor]
        cursor += 1
        start, end = float(row["start"]), float(row["end"])
        inside = [t for t in tokens if t["e"] > start + 1e-6 and t["s"] < end - 1e-6]
        actual = norm("".join(str(t.get("w", "")) for t in inside))
        planned = norm(row.get("text", ""))
        coverage = (len(actual) / len(planned)) if planned else 0.0
        similarity = difflib.SequenceMatcher(None, planned, actual).ratio() if planned else 0.0
        segments.append({
            "index": len(segments), "start": row["start"], "end": row["end"],
            "dur": row["dur"], "mode": row.get("mode") or "-",
            "need_manual": row["need_manual"],
            "planned": row.get("text", ""), "actual": actual,
            "similarity": round(similarity, 3), "word_coverage": round(coverage, 3),
            "issues": issues.get(row_index, []),
        })

    clean = [s["index"] for s in segments
             if s["index"] is not None and not s["issues"] and not s["need_manual"]
             and s["word_coverage"] >= 0.5]
    out = {"ok": bool(clean), "clean_segments": clean, "skipped": skipped,
           "segments": segments,
           "note": "clean_segments 为审计零问题且词表覆盖达标的候选，可直接写进 picks.json"}
    Path(args.report).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    print(f"probe: {len(segments)} 段 / 干净可用 {len(clean)} 段 / 淘汰 "
          f"{len(skipped)} 段 -> {args.report}")
    for item in segments:
        mark = "OK " if item["index"] in clean else "-- "
        print(f"  {mark}[{str(item['index']):>3}] {item['start']:8.2f}-{item['end']:8.2f} "
              f"{item['dur']:5.2f}s cov={item['word_coverage']:.2f} "
              f"sim={item['similarity']:.2f} {item['actual'][:36]}")


if __name__ == "__main__":
    main()
