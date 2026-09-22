# -*- coding: utf-8 -*-
"""Validate one timeline or several hook+body combinations locally."""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
from badvocab import hit, review_hits  # noqa: E402
from textnorm import content_rejection, context_dependent_start, incomplete_ending  # noqa: E402
from agent_video.engine.validation_policy import (ALIGNMENT_EXPANSION_MARGIN,
                                                   shared_issues)  # noqa: E402

ALLOWED_ROLES = {"hook", "result", "pain", "proof", "fit", "material", "craft",
                 "color", "styling", "scene", "demo", "close", "bridge",
                 "personality", "story", "reaction", "visual"}
COMPOSITION_CLAIM = re.compile(r"(?:百分百|100)\s*(?:的)?\s*(?:羊毛|美丽奴|澳毛)|(?:美丽奴|澳毛)")

# 时长来自「两个三位小数相减」，浮点误差可达 1e-14：77.71-76.51 会算出
# 1.1999999999999886，比 1.2 小一点点就被判不合格。门禁比较一律带这个容差，
# 否则合法片段会因为第 15 位有效数字被拦下（实测卡过 1.20s 的段）。
EPS = 1e-6


def normalized(text):
    return "".join(c.lower() for c in (text or "") if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def parse_mapping(values, kind):
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key or not os.path.isfile(path):
            raise SystemExit(f"Invalid --{kind}: {value!r}; expected KEY=existing-file")
        result[key] = os.path.abspath(path)
    return result


def media_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    return float(result.stdout.strip())


def load_timeline(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"Timeline must be a JSON array: {path}")
    return data


def issue(code, message, level="error", **extra):
    return {"level": level, "code": code, "message": message, **extra}


def validate_rows(rows, sources, min_total, max_total, min_segments, max_segments,
                  min_segment, max_segment, max_demo_segment, require_structure):
    issues, durations, texts, seen_ranges = shared_issues(rows), {}, [], []
    composition_rows = {}
    total, demo_count = 0.0, 0
    for index, row in enumerate(rows):
        source_id = str(int(row.get("src", 1)))
        start, end = float(row.get("start", -1)), float(row.get("end", -1))
        text, dur = row.get("text", ""), end - start
        total += max(0.0, dur)
        if source_id not in sources:
            issues.append(issue("missing_source", f"segment {index}: source {source_id} missing",
                                segment=index, src=source_id))
        else:
            if source_id not in durations:
                durations[source_id] = media_duration(sources[source_id])
            if start < 0 or end > durations[source_id] + 0.05 or end <= start:
                issues.append(issue("out_of_bounds", f"segment {index}: {start:.3f}-{end:.3f}",
                                    segment=index, src=source_id))
        role = row.get("role")
        if role and role not in ALLOWED_ROLES:
            issues.append(issue("invalid_role", f"segment {index}: {role}",
                                segment=index, role=role))
        is_demo = role == "demo"
        demo_count += int(is_demo)
        allowed_max = max_demo_segment if is_demo else max_segment
        if dur > allowed_max + EPS:
            level = ("warning" if dur <= allowed_max + ALIGNMENT_EXPANSION_MARGIN + EPS
                     else "error")
            issues.append(issue("segment_too_long", f"segment {index}: {dur:.2f}s",
                                level=level, segment=index, duration=round(dur, 3),
                                maximum=allowed_max))
        if dur < min_segment - EPS:
            issues.append(issue("segment_too_short", f"segment {index}: {dur:.2f}s",
                                segment=index, duration=round(dur, 3)))
        banned = hit(text)
        if banned:
            issues.append(issue("banned_word", f"segment {index}: {banned}",
                                segment=index, match=banned))
        review = review_hits(text)
        if review:
            issues.append(issue("context_review", f"segment {index}: {', '.join(review)}",
                                level="warning", segment=index, matches=review))
        ending = incomplete_ending(text)
        if ending:
            issues.append(issue("incomplete_sentence",
                                f"segment {index}: dangling connector {ending}",
                                segment=index, ending=ending))
        else:
            rejection = content_rejection(text)
            if rejection:
                issues.append(issue(rejection,
                                    f"segment {index}: unusable spoken text ({rejection})",
                                    segment=index))
        norm = normalized(text)
        if COMPOSITION_CLAIM.search(norm):
            product = str(row.get("product") or "__whole_video__").strip()
            composition_rows.setdefault(product, []).append(index)
        for other_index, other in texts:
            if norm and (norm == other or (min(len(norm), len(other)) >= 6 and
                                           difflib.SequenceMatcher(None, norm, other).ratio() >= 0.88)):
                issues.append(issue("duplicate_text", f"segments {other_index}/{index}",
                                    segments=[other_index, index]))
                break
        texts.append((index, norm))
        for other_index, other_source, other_start, other_end in seen_ranges:
            overlap = min(end, other_end) - max(start, other_start)
            if source_id == other_source and overlap > 0.2:
                issues.append(issue("overlapping_source_range",
                                    f"segments {other_index}/{index}: {overlap:.2f}s",
                                    segments=[other_index, index], overlap=round(overlap, 3)))
                break
        seen_ranges.append((index, source_id, start, end))
    for product, indexes in composition_rows.items():
        if len(indexes) > 1:
            issues.append(issue(
                "repeated_composition_claim",
                "composition/material claim may appear only once per product",
                segments=indexes, product=None if product == "__whole_video__" else product))
    # A timeline is concatenated in this exact order.  Short individual picks can still
    # create a 10+ second static opening when they come from one uninterrupted source run.
    run_start = 0
    for index in range(1, len(rows) + 1):
        contiguous = False
        if index < len(rows):
            prev, current = rows[index - 1], rows[index]
            # 必须带下界：只有「顺着源时间往下走」才算同一镜头。按叙事重排的 picks
            # 会出现 current.start < prev.end（往回跳 = 一次硬切），原实现只判
            # <=0.60，把负间隙也算成连续，于是整条重排时间线被并成一段超长 run，
            # 任何多钩子+正文的正常交付都会假失败。
            contiguous = (int(prev.get("src", 1)) == int(current.get("src", 1)) and
                          -0.05 <= float(current.get("start", 0)) - float(prev.get("end", 0))
                          <= 0.60)
        if contiguous:
            continue
        run_seconds = sum(float(rows[j].get("end", 0)) - float(rows[j].get("start", 0))
                          for j in range(run_start, index))
        if run_seconds > 10.0 + EPS:
            issues.append(issue("continuous_source_run",
                                f"segments {run_start}-{index - 1} form an unbroken {run_seconds:.2f}s source run",
                                segments=[run_start, index - 1], duration=round(run_seconds, 3)))
        elif run_seconds > 5.5 + EPS:
            issues.append(issue("long_natural_run",
                                f"segments {run_start}-{index - 1} preserve a natural {run_seconds:.2f}s source run",
                                level="warning", segments=[run_start, index - 1],
                                duration=round(run_seconds, 3)))
        run_start = index
    if total < min_total - EPS:
        issues.append(issue("soft_duration_short",
                            f"total {total:.2f}s below soft target {min_total:.2f}s",
                            level="warning", duration=round(total, 3)))
    if total > max_total + EPS:
        issues.append(issue("duration_too_long",
                            f"total {total:.2f}s exceeds {max_total:.2f}s",
                            duration=round(total, 3)))
    if len(rows) < min_segments:
        issues.append(issue("too_few_segments",
                            f"{len(rows)} segments below minimum {min_segments}; "
                            "a cut must be composed of 2-5s spoken units"))
    if len(rows) > max_segments:
        issues.append(issue("too_many_segments", f"{len(rows)} segments; maximum {max_segments}"))
    if demo_count > 3:
        issues.append(issue("too_many_long_demos",
                            f"{demo_count} demo segments; check that each adds a new action or result",
                            level="warning"))
    if require_structure and rows:
        roles = [row.get("role") for row in rows]
        opening_connector = context_dependent_start(str(rows[0].get("text") or ""))
        if opening_connector:
            issues.append(issue("context_dependent_opening",
                                f"opening starts with context-dependent phrase {opening_connector}",
                                level="warning", segment=0, match=opening_connector))
        if roles[0] not in {"hook", "result", "pain", "proof", "demo", "personality",
                            "story", "reaction", "visual"}:
            issues.append(issue("weak_opening_role", "opening may lack a clear viewing reason",
                                level="warning"))
        if "proof" not in roles and "demo" not in roles:
            issues.append(issue("missing_proof", "selling-led edits benefit from proof or demo",
                                level="warning"))
        if "close" not in roles:
            issues.append(issue("missing_close", "no explicit close; a natural ending is allowed",
                                level="warning"))
        if not any(role in {"fit", "pain", "scene", "styling"} for role in roles):
            issues.append(issue("missing_customer_relevance",
                                "selling-led edits benefit from fit, pain, scene, or styling content",
                                level="warning"))
        opening = roles[:min(3, len(roles))]
        if opening and all(role in {"bridge", "material", "craft", "color"}
                           for role in opening):
            issues.append(issue("abstract_opening_sequence",
                                "opening may be only background, material, craft, or color",
                                level="warning"))
        run_start = 0
        for i in range(1, len(roles) + 1):
            if i < len(roles) and roles[i] == roles[run_start]:
                continue
            run_seconds = sum(float(rows[j].get("end", 0)) - float(rows[j].get("start", 0))
                              for j in range(run_start, i))
            if i - run_start > 2 and run_seconds > 8.0:
                issues.append(issue("role_cluster",
                                    f"{roles[run_start]} repeats {i-run_start} times / "
                                    f"{run_seconds:.2f}s",
                                    level="warning",
                                    role=roles[run_start], segments=[run_start, i - 1],
                                    duration=round(run_seconds, 3)))
            run_start = i
        close_index = next((i for i, role in enumerate(roles) if role == "close"), None)
        if close_index is not None and close_index != len(roles) - 1:
            issues.append(issue("content_after_close", "close must be the final segment"))
        missing_roles = [i for i, role in enumerate(roles) if not role]
        if missing_roles:
            issues.append(issue("missing_roles", "all segments need role metadata",
                                segments=missing_roles))
    average = total / len(rows) if rows else 0.0
    if average > 5.0:
        # 短句合并后 1.5–18s 的自然口播单元会拉高平均段长，这是预期行为；
        # 单段上限（max_segment）与总时长目标已负责质量约束，故平均段长只作提示。
        issues.append(issue("average_segment_too_long", f"average {average:.2f}s",
                            level="warning", duration=round(average, 3)))
    unique, seen = [], set()
    for item in issues:
        identity = (item.get("code"), tuple(item.get("segments") or []), item.get("segment"))
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return {"segments": len(rows), "duration": round(total, 3),
            "average_segment": round(average, 3), "issues": unique}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("timeline", nargs="?")
    parser.add_argument("--body")
    parser.add_argument("--hook", action="append", default=[])
    parser.add_argument("--src", action="append", default=[])
    parser.add_argument("--min-total", type=float, default=70.0)
    parser.add_argument("--max-total", type=float, default=120.0)
    parser.add_argument("--min-segments", type=int, default=18)
    parser.add_argument("--max-segments", type=int, default=32)
    parser.add_argument("--min-segment", type=float, default=1.2)
    parser.add_argument("--max-segment", type=float, default=8.0)
    parser.add_argument("--max-demo-segment", type=float, default=8.0)
    parser.add_argument("--require-structure", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    sources, hooks = parse_mapping(args.src, "src"), parse_mapping(args.hook, "hook")
    if args.timeline and (args.body or hooks):
        raise SystemExit("Use either TIMELINE or --body/--hook mode")
    if not args.timeline and not args.body:
        raise SystemExit("Provide TIMELINE or --body (hooks are optional)")
    results = {}
    if args.timeline:
        results["timeline"] = validate_rows(load_timeline(args.timeline), sources,
                                             args.min_total, args.max_total,
                                             args.min_segments, args.max_segments,
                                             args.min_segment, args.max_segment,
                                             args.max_demo_segment, args.require_structure)
    else:
        body = load_timeline(args.body)
        if hooks:
            for name, path in hooks.items():
                results[name] = validate_rows(load_timeline(path) + body, sources,
                                              args.min_total, args.max_total,
                                              args.min_segments, args.max_segments,
                                              args.min_segment, args.max_segment,
                                              args.max_demo_segment, args.require_structure)
        else:
            results["body"] = validate_rows(body, sources,
                                             args.min_total, args.max_total,
                                             args.min_segments, args.max_segments,
                                             args.min_segment, args.max_segment,
                                             args.max_demo_segment, args.require_structure)
    errors = sum(sum(item["level"] == "error" for item in result["issues"])
                 for result in results.values())
    warnings = sum(sum(item["level"] == "warning" for item in result["issues"])
                   for result in results.values())
    report = {"ok": errors == 0, "errors": errors, "warnings": warnings,
              "combinations": results}
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    summaries = ", ".join(f"{name}:{value['duration']:.2f}s/{len(value['issues'])} issues"
                          for name, value in results.items())
    print(f"timeline validation {'passed' if not errors else 'failed'}: {summaries}; "
          f"{warnings} warnings -> {args.report}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
