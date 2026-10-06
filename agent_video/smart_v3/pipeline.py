from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from .ai import JsonAI, validate_profiles, validate_review

# These conservative rules belong exclusively to V3. Attraction is judged in S3.
INVALID = re.compile(r"(场控|管理员|库存只?剩|库存只有|快递|发货|物流|拍一号链接|赶紧下单|最后一单|扣个[一1])")
DEPENDENT = re.compile(r"^(所以|但是|然后|这个也|它也|刚才|前面|那样|这样)")


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout


def restore_sentences(words, ai):
    raw = ai("sentences", {"words": [{"id": i, "text": w["text"]} for i, w in enumerate(words)]})
    ranges = raw.get("sentences")
    tail = raw.get("tail_start")
    if not isinstance(ranges, list) or type(tail) is not int or not 0 <= tail <= len(words):
        raise ValueError("Invalid ASR sentence boundaries")
    rows, cursor = [], 0
    for item in ranges:
        first, last = item.get("first"), item.get("last")
        if type(first) is not int or type(last) is not int or first != cursor or not first <= last < tail:
            raise ValueError("Invalid/non-contiguous ASR sentence boundaries")
        rows.append({"start": words[first]["start"], "end": words[last]["end"],
                     "text": "".join(w["text"] for w in words[first:last + 1]) + "。"})
        cursor = last + 1
    if cursor != tail:
        raise ValueError("Model omitted ASR words")
    return rows, words[tail:]


def understand(source, workspace, transcript=None):
    duration = float(command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", source]).strip())
    if transcript:
        rows = json.loads(Path(transcript).read_text(encoding="utf-8-sig"))
    else:
        # Third-party ASR only; no Legacy engine, glossary, prompts or cache imports.
        from faster_whisper import WhisperModel
        model = WhisperModel(os.environ.get("SMART_V3_ASR_MODEL", "small"), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(source, language="zh", word_timestamps=True, vad_filter=True)
        rows = []
        pending = []
        for segment in segments:
            for word in segment.words or []:
                pending.append(word)
                if re.search(r"[。！？!?]", word.word):
                    rows.append({"start": pending[0].start, "end": pending[-1].end,
                                 "text": "".join(w.word for w in pending)})
                    pending = []
        if pending and not rows:
            words = [{"start": w.start, "end": w.end, "text": w.word} for w in pending]
            (workspace / "asr-words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
            rows, rejected = restore_sentences(words, JsonAI())
            pending = [SimpleNamespace(start=w["start"], end=w["end"], word=w["text"]) for w in rejected]
        if pending:
            # Keep the rejected tail observable without admitting a cut-off sentence.
            (workspace / "asr-rejected-tail.json").write_text(json.dumps({
                "reason": "incomplete_asr_tail", "start": pending[0].start,
                "end": pending[-1].end, "text": "".join(w.word for w in pending)
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        (workspace / "asr-complete-sentences.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if not isinstance(rows, list) or not rows:
        raise ValueError("No complete timestamped sentences")
    units = []
    previous_end = 0.0
    for i, row in enumerate(rows):
        start, end = float(row["start"]), float(row["end"])
        text = row["text"].strip()
        if not text or not all(math.isfinite(n) for n in (start, end)) or not previous_end <= start < end <= duration + .05:
            raise ValueError("Invalid/non-monotonic transcript boundaries")
        if not re.search(r"[。！？!?]$", text):
            raise ValueError("Transcript must contain complete punctuated sentences; incomplete ASR tail rejected")
        previous_end = end
        dependent = bool(DEPENDENT.search(text))
        units.append({"id": f"u{i}", "start": start, "end": end, "text": text,
                      "independent_start": not dependent,
                      "previous_dependency": [f"u{i-1}"] if dependent and i else [],
                      "next_dependency": [], "visual": {"scene_id": None, "shot_changes": None,
                      "status": "not_analyzed"}})
    return units


def coarse_filter(units, product=""):
    kept, removed, seen = [], [], set()
    for unit in units:
        normalized = re.sub(r"[\W_]+", "", unit["text"])
        reason = "operational_content" if INVALID.search(unit["text"]) else None
        if normalized in seen:
            reason = "exact_duplicate"
        if not normalized:
            reason = "empty_asr"
        if re.search(r"(.)\1{7,}", normalized):
            reason = "obvious_asr_repetition"
        # Explicit product switches only. Styling/comparison mentions remain candidates.
        other = re.search(r"(?:接下来介绍|现在介绍的是|下一款是)(?:这件|这条|这款)?(毛衣|衬衫|连衣裙|裤子|外套|半身裙)", unit["text"])
        if other and product and product not in unit["text"] and other.group(1) not in product:
            reason = "explicit_other_product"
        seen.add(normalized)
        (removed if reason else kept).append({**unit, "filter_reason": reason})
    return {"kept": kept, "removed": removed}


def profile(ai, units, product):
    profiles = validate_profiles(ai("profile", {"product": product, "units": units}), units)
    by_id = {p["id"]: p for p in profiles}
    return [{**u, **by_id[u["id"]]} for u in units]


def duration(units):
    return round(sum(u["end"] - u["start"] for u in units), 3)


def audit(units, low, high):
    issues = []
    if not units:
        return [{"unit_id": "", "reason": "empty timeline"}]
    first = units[0]
    if not first["independent"] or not first["independent_start"] or first["dependency"] or first["hook_strength"] < .5:
        issues.append({"unit_id": first["id"], "reason": "weak/dependent opening"})
    if not low <= duration(units) <= high:
        issues.append({"unit_id": first["id"], "reason": "duration outside target"})
    seen, claims, weak_run, scene_seconds, last_scene = set(), set(), 0, 0, None
    elapsed, product_return = 0, None
    for u in units:
        missing = (set(u["dependency"]) | set(u["previous_dependency"])) - seen
        if missing:
            issues.append({"unit_id": u["id"], "reason": "missing context: " + ",".join(sorted(missing))})
        if u["claim_cluster"] in claims:
            issues.append({"unit_id": u["id"], "reason": "repeated information point"})
        weak_run = weak_run + 1 if u["strength"] < .4 else 0
        if weak_run >= 2:
            issues.append({"unit_id": u["id"], "reason": "consecutive weak sentences"})
        scene = u["visual"].get("scene_id")
        scene_seconds = scene_seconds + u["end"] - u["start"] if scene is not None and scene == last_scene else u["end"] - u["start"]
        if scene is not None and scene_seconds > 25:
            issues.append({"unit_id": u["id"], "reason": "same scene over 25 seconds"})
        last_scene = scene
        if product_return is None and u["product_relevance"] >= .6:
            product_return = elapsed
        elapsed += u["end"] - u["start"]
        seen.add(u["id"])
        claims.add(u["claim_cluster"])
    if product_return is None or product_return > 12:
        issues.append({"unit_id": first["id"], "reason": "returns to product too late"})
    return issues


def film_score(units, low, high):
    n = len(units) or 1
    repetition = 1 - len({u["claim_cluster"] for u in units}) / n
    jumps = sum(a["topic"] != b["topic"] for a, b in zip(units, units[1:])) / max(1, n - 1)
    density = sum(u["strength"] for u in units) / n
    consistency = sum(u["product_relevance"] for u in units) / n
    hook = units[0]["hook_strength"] if units else 0
    predicted = max(0, min(1, .25 * hook + .35 * density + .25 * consistency + .15 * (1 - jumps) - repetition))
    violations = audit(units, low, high)
    return {"opening": hook, "information_repetition_rate": repetition, "topic_jump_rate": jumps,
            "content_density": density, "theme_consistency": consistency,
            "predicted_retention": predicted, "prediction_is_human_feedback": False,
            "total": round(predicted * 100 - 20 * len(violations), 3), "violations": violations,
            "visual_status": "unknown" if any(u["visual"]["scene_id"] is None for u in units) else "available"}


def arrange(units, low, high):
    available = [u for u in units if u["usable"] and u["product_relevance"] >= .4]
    if not available:
        raise ValueError("No usable product sentences")
    # Focus comes from candidate content, never a fixed hook/pain/proof/close template.
    focus = Counter(r for u in available for r in u["role"] if r in {"result", "styling", "pain", "material", "scene", "fit"})
    dominant = focus.most_common(1)[0][0] if focus else "mixed"
    names = {"result": "上身效果型", "styling": "穿搭型", "pain": "痛点解决型", "material": "面料品质型", "scene": "情绪种草型", "fit": "版型展示型", "mixed": "混合型"}
    openings = sorted([u for u in available if u["independent"] and u["independent_start"] and not u["dependency"] and not u["previous_dependency"] and u["hook_strength"] >= .5], key=lambda u: -u["hook_strength"])
    candidates, signatures = [], set()
    by_id = {u["id"]: u for u in available}
    # Beam search enforces context and unique claims while exploring alternative orders.
    for strategy in ("focus", "density", "continuity"):
        beam = [[u] for u in openings[:8]]
        completed = []
        for _ in range(len(available)):
            expanded = []
            for seq in beam:
                if low <= duration(seq) <= high:
                    completed.append(seq)
                ids, claims = {u["id"] for u in seq}, {u["claim_cluster"] for u in seq}
                for u in available:
                    deps = set(u["dependency"]) | set(u["previous_dependency"])
                    if u["id"] in ids or u["claim_cluster"] in claims or not deps <= ids or not deps <= by_id.keys():
                        continue
                    if duration(seq + [u]) <= high:
                        expanded.append(seq + [u])
            def rank(seq):
                quality = film_score(seq, 0, high)["total"]
                if strategy == "focus":
                    quality += sum(dominant in u["role"] for u in seq) * 3
                elif strategy == "continuity":
                    quality += sum(a["topic"] == b["topic"] for a, b in zip(seq, seq[1:])) * 3
                else:
                    quality += sum(u["strength"] for u in seq) * 3
                return quality + min(duration(seq), low) * .3
            beam = sorted(expanded, key=rank, reverse=True)[:60]
            if not beam:
                break
        for seq in sorted(completed, key=rank, reverse=True):
            signature = tuple(u["id"] for u in seq)
            if signature in signatures:
                continue
            signatures.add(signature)
            candidates.append({"id": f"c{len(candidates)}", "structure": names[dominant],
                               "strategy": strategy, "unit_ids": list(signature), "duration": duration(seq),
                               "score": film_score(seq, low, high)})
            break
    # Fill from remaining explored orders if a strategy selected an existing order.
    for seq in sorted(completed, key=lambda s: film_score(s, low, high)["total"], reverse=True):
        signature = tuple(u["id"] for u in seq)
        if len(candidates) >= 3:
            break
        if signature not in signatures:
            signatures.add(signature)
            candidates.append({"id": f"c{len(candidates)}", "structure": names[dominant], "strategy": "alternative",
                               "unit_ids": list(signature), "duration": duration(seq), "score": film_score(seq, low, high)})
    if len(candidates) < 3:
        raise ValueError("Insufficient material for 3 distinct complete timelines within target")
    return {"candidates": candidates, "selected": max(candidates, key=lambda c: c["score"]["total"])["id"]}


def review(ai, units, compilation, low, high, product):
    by_id = {u["id"]: u for u in units}
    chosen = next(c for c in compilation["candidates"] if c["id"] == compilation["selected"])
    initial = chosen["unit_ids"][:]
    timeline = [by_id[i] for i in initial]
    reports, replacements = [], []
    for attempt in range(3):
        model = validate_review(ai("review", {"product": product, "timeline": timeline,
                                "visual_status": "not_analyzed"}), {u["id"] for u in timeline})
        issues = audit(timeline, low, high) + model["issues"]
        reports.append({"attempt": attempt, "model": model, "issues": issues, "duration": duration(timeline)})
        if not issues and model["passed"]:
            return {"passed": True, "initial_unit_ids": initial, "final_unit_ids": [u["id"] for u in timeline],
                    "timeline": timeline, "reports": reports, "replacements": replacements,
                    "generated_seconds": duration(timeline), "visual_assessment": "not_analyzed"}
        if attempt == 2:
            break
        changed = False
        for issue in issues:
            index = next((i for i, u in enumerate(timeline) if u["id"] == issue["unit_id"]), None)
            if index is None:
                continue
            old = timeline[index]
            options = sorted([u for u in units if u["usable"] and u["id"] not in {v["id"] for v in timeline}
                              and u["product_relevance"] >= .4
                              and (u["topic"] == old["topic"] or set(u["role"]) & set(old["role"]))], key=lambda u: -u["strength"])
            for new in options:
                trial = timeline[:index] + [new] + timeline[index+1:]
                if not audit(trial, low, high):
                    timeline = trial
                    replacements.append({"from": old["id"], "to": new["id"], "reason": issue["reason"], "attempt": attempt})
                    changed = True
                    break
        if not changed:
            # Alternative whole-film candidate of the same content-derived type.
            alternatives = [c for c in compilation["candidates"] if c["structure"] == chosen["structure"] and c["unit_ids"] != [u["id"] for u in timeline]]
            if alternatives:
                alt = sorted(alternatives, key=lambda c: -c["score"]["total"])[min(attempt, len(alternatives)-1)]
                replacements.append({"from_candidate": chosen["id"], "to_candidate": alt["id"], "reason": "whole-film review failed"})
                timeline = [by_id[i] for i in alt["unit_ids"]]
                chosen = alt
    return {"passed": False, "initial_unit_ids": initial, "final_unit_ids": [u["id"] for u in timeline],
            "timeline": timeline, "reports": reports, "replacements": replacements,
            "generated_seconds": duration(timeline), "visual_assessment": "not_analyzed"}


def render(source, workspace, timeline):
    parts = []
    for i, unit in enumerate(timeline):
        part = workspace / f"part-{i:03}.mp4"
        command(["ffmpeg", "-v", "error", "-y", "-ss", str(unit["start"]), "-i", source,
                 "-t", str(unit["end"] - unit["start"]), "-map", "0:v:0", "-map", "0:a:0?",
                 "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-avoid_negative_ts", "make_zero", str(part)])
        parts.append(part)
    listing = workspace / "concat.txt"
    listing.write_text("\n".join(f"file '{p.name}'" for p in parts), encoding="utf-8")
    output = workspace / "film.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(output)])
    actual = float(command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(output)]))
    if abs(actual - duration(timeline)) > max(.5, len(timeline) * .1):
        raise ValueError("Rendered duration diverges from reviewed timeline")
    return output
