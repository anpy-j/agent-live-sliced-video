# -*- coding: utf-8 -*-
"""Audit timeline boundaries against source-side word timestamps.

Single source:
  python audit_bounds.py timeline.json words.json --report audit.json
Multi source:
  python audit_bounds.py timeline.json --word-map 1=src1_words.json \
      --word-map 2=src2_words.json --report audit.json
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 词表统一来自 badvocab（受 DOUYIN_VOCAB_PROFILE 控制）；这里不再各写一份写死的
# 女装词表 —— 原实现藏在 audit 里，换类目（鞋、羽绒服）就失效，而且和「唯一合规词源」
# 的说法矛盾。两个常量都可用命令行覆盖。
from badvocab import hit  # noqa: E402
from badvocab import SECONDARY_ATTRIBUTES, SECONDARY_PRODUCTS  # noqa: E402
from textnorm import cn_num  # noqa: E402


LIVE_COORDINATION_RE = re.compile(r"要[^，。！？]{0,8}(?:ok|OK)|(?:ok|OK)[^，。！？]{0,8}(?:要|拿)")

# 静音余量由两个三位小数相减得到，浮点误差可达 1e-14；比较一律带容差，
# 否则刚好 0.30s 的片段会被误判成头/尾静音。
EPS = 1e-6

# Editorial preferences should remain visible in the audit report without
# blocking an otherwise executable timeline.
SOFT_ISSUE_TYPES = {"secondary_product_detail", "spoken_number_variance"}

# 数字字形：同一段口播在不同来源里字形不同（字幕/计划写「3050」，词级 ASR 写
# 「三十五十」）。纯数字字形差异不代表选错区间，不应阻塞渲染。
NUMERAL_CHARS = frozenset("0123456789零一二三四五六七八九十百千万亿两")


def issue_level(item):
    return "warning" if item.get("type") in SOFT_ISSUE_TYPES else "error"


def norm(text):
    """数字先转中文再比对：字幕写「150斤」、转写出「一百五十斤」时，
    不归一就会把正确选段误判成 mismatch（实测反复触发无意义的换段）。
    与 cuts.py 共用 textnorm，保证两侧归一方式一致。"""
    return "".join(c.lower() for c in cn_num(text)
                   if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def without_numerals(normalized):
    """去掉全部数字字形，用于判断差异是否只来自数字写法。"""
    return "".join(c for c in normalized if c not in NUMERAL_CHARS)


def numerals_only_variance(expected, actual, threshold):
    """口播与计划文本除了数字写法外是否一致。

    norm() 只能归一整段阿拉伯数字（如 150→一百五十），无法处理两侧把同一串
    数字切成不同单位的情况（计划「3050」vs 语音「三十五十」）。渲染使用原声，
    text 不参与画面，因此这种纯字形差异降级为提示，不再阻塞整条成片。
    """
    kept_expected, kept_actual = without_numerals(expected), without_numerals(actual)
    if len(kept_expected) < 2 or len(kept_actual) < 2:
        return False
    if kept_expected == kept_actual:
        return True
    return difflib.SequenceMatcher(None, kept_expected, kept_actual).ratio() >= threshold


def diagnose(coverage, similarity):
    """把「相似度不足」直接翻译成可执行的下一步。

    原来只报 similarity < 0.86，不说是「这段压根没有词」还是「文案写错了」，
    于是必须人工再做一轮词级探测才能定位根因（实测多耗了两轮全量重跑）。"""
    if coverage < 0.5:
        return (f"词表覆盖过低（{coverage:.2f}）：该区间疑似静音/纯 BGM 或 ASR 幻觉，"
                "应换段，不要重复调参重试")
    if coverage > 1.8:
        return (f"实际口播远长于计划文本（{coverage:.2f}）：切点吃进了相邻句，"
                "建议把窗口收窄到整句边界内")
    if similarity < 0.4:
        return "口播与计划文本几乎不匹配：多为选错区间或 ASR 幻觉，建议换段"
    return "口播与计划文本不符：用 actual 字段回写 picks.json 的 text 后重跑"


def load_words(path):
    with open(path, encoding="utf-8") as handle:
        return sorted(json.load(handle), key=lambda row: row["s"])


def parse_map(values):
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.isdigit() or not os.path.isfile(path):
            raise SystemExit(f"Invalid --word-map: {value!r}; expected ID=existing-json")
        result[int(key)] = load_words(path)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("timeline")
    parser.add_argument("words", nargs="?", help="Legacy single-source words.json")
    parser.add_argument("--word-map", action="append", default=[])
    parser.add_argument("--word-map-json",
                        help="JSON object written by cuts.py: source id -> words file")
    parser.add_argument("--report", required=True)
    parser.add_argument("--main-product")
    parser.add_argument("--allow-manual", action="store_true")
    parser.add_argument("--min-text-similarity", type=float, default=0.86)
    parser.add_argument("--require-binding", action="store_true")
    parser.add_argument("--secondary-products",
                        help="副商品词表（正则片段），默认取词表配置")
    parser.add_argument("--secondary-attributes",
                        help="副商品属性词表（正则片段），默认取词表配置")
    parser.add_argument("--allowed-product", action="append", default=[],
                        help="本任务合法商品；可重复，命中后不按副商品拦截")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    secondary_products = args.secondary_products or SECONDARY_PRODUCTS
    secondary_attributes = args.secondary_attributes or SECONDARY_ATTRIBUTES
    with open(args.timeline, encoding="utf-8") as handle:
        timeline = json.load(handle)
    word_map = parse_map(args.word_map)
    if args.word_map_json:
        with open(args.word_map_json, encoding="utf-8") as handle:
            manifest = json.load(handle)
        meta = manifest.get("_meta") or {}
        if args.require_binding:
            with open(args.timeline, "rb") as handle:
                timeline_hash = hashlib.sha256(handle.read()).hexdigest()
            if meta.get("timeline_sha256") != timeline_hash or \
                    int(meta.get("segments", -1)) != len(timeline):
                raise SystemExit("Word map is stale or does not cover this timeline")
        base = os.path.dirname(os.path.abspath(args.word_map_json))
        for key, path in manifest.items():
            if str(key).startswith("_"):
                continue
            resolved = path if os.path.isabs(path) else os.path.join(base, path)
            if not os.path.isfile(resolved):
                raise SystemExit(f"Missing words file in map: {resolved}")
            word_map.setdefault(int(key), load_words(resolved))
    if args.words:
        word_map.setdefault(1, load_words(args.words))
    if not word_map:
        raise SystemExit("Provide words.json or one or more --word-map ID=words.json")

    issues = []
    for index, row in enumerate(timeline):
        source_id = int(row.get("src", 1))
        words = word_map.get(source_id)
        if words is None:
            issues.append({"segment": index, "src": source_id, "type": "missing_word_map"})
            continue
        start = float(row.get("start", row.get("gstart")))
        end = float(row.get("end", row.get("gend")))
        inside = [word for word in words if word["e"] > start + 1e-6 and word["s"] < end - 1e-6]
        if row.get("need_manual") and not args.allow_manual and not row.get("manual_approved"):
            issues.append({"segment": index, "src": source_id,
                           "type": "unapproved_manual_boundary"})
        partial = [word for word in inside
                   if word["s"] < start - 0.005 or word["e"] > end + 0.005]
        for word in partial:
            issues.append({"segment": index, "src": source_id, "type": "cut_inside_token",
                           "token": word.get("w", ""), "token_start": word["s"],
                           "token_end": word["e"]})
        if not inside:
            issues.append({"segment": index, "src": source_id, "type": "no_words",
                           "word_coverage": 0.0,
                           "diagnosis": "词表在该区间为空：多为静音、纯 BGM 或 ASR 失败区，"
                                        "应换段，不要重复调参重试"})
            continue
        actual = "".join(str(word.get("w", "")) for word in inside)
        expected_norm, actual_norm = norm(row.get("text", "")), norm(actual)
        coverage = (len(actual_norm) / len(expected_norm)) if expected_norm else 1.0
        similarity = difflib.SequenceMatcher(None, expected_norm, actual_norm).ratio() \
            if expected_norm else 1.0
        if expected_norm and expected_norm in actual_norm and expected_norm != actual_norm:
            where = actual_norm.index(expected_norm)
            prefix, suffix = actual_norm[:where], actual_norm[where + len(expected_norm):]
            if prefix or suffix:
                issues.append({"segment": index, "src": source_id,
                               "type": "unexpected_spoken_edge",
                               "prefix": prefix[:30], "suffix": suffix[:30],
                               "word_coverage": round(coverage, 3),
                               "diagnosis": "切点吃进了相邻内容：把窗口收窄到整句边界内"})
        elif expected_norm and similarity < args.min_text_similarity:
            if numerals_only_variance(expected_norm, actual_norm,
                                      args.min_text_similarity):
                issues.append({"segment": index, "src": source_id,
                               "type": "spoken_number_variance",
                               "similarity": round(similarity, 4),
                               "word_coverage": round(coverage, 3),
                               "actual": actual_norm[:100]})
            else:
                issues.append({"segment": index, "src": source_id,
                               "type": "spoken_text_mismatch",
                               "similarity": round(similarity, 4),
                               "word_coverage": round(coverage, 3),
                               "diagnosis": diagnose(coverage, similarity),
                               "actual": actual_norm[:100]})
        banned = hit(actual)
        if banned:
            issues.append({"segment": index, "src": source_id,
                           "type": "actual_banned_word", "match": banned})
        if LIVE_COORDINATION_RE.search(actual):
            issues.append({"segment": index, "src": source_id,
                           "type": "live_coordination", "actual": actual_norm[:100]})
        allowed_here = any(product and product in actual for product in args.allowed_product)
        if args.main_product and not allowed_here and not re.search(args.main_product, secondary_products):
            secondary = re.search(secondary_products, actual)
            attribute = re.search(secondary_attributes, actual)
            legal_secondary = bool(secondary and any(
                secondary.group(0) in product for product in args.allowed_product))
            if secondary and attribute and not legal_secondary:
                issues.append({"segment": index, "src": source_id,
                               "type": "secondary_product_detail",
                               "product": secondary.group(0),
                               "attribute": attribute.group(0)})
        lead = inside[0]["s"] - start
        tail = end - max(word["e"] for word in inside)
        if lead > 0.30 + EPS:
            issues.append({"segment": index, "src": source_id, "type": "head_silence",
                           "seconds": round(lead, 3)})
        if tail > 0.30 + EPS:
            issues.append({"segment": index, "src": source_id, "type": "tail_silence",
                           "seconds": round(tail, 3)})
        if args.verbose:
            print(f"{index:02d} src{source_id} {start:.3f}-{end:.3f} "
                  f"{''.join(str(word.get('w', '')) for word in inside)}")

    for item in issues:
        item.setdefault("level", issue_level(item))
    errors = [item for item in issues if item["level"] == "error"]
    warnings = [item for item in issues if item["level"] == "warning"]
    by_type = {}
    for item in issues:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    low_coverage = [{"segment": item["segment"], "type": item["type"],
                     "word_coverage": item["word_coverage"]}
                    for item in issues
                    if item.get("word_coverage") is not None
                    and item["word_coverage"] < 0.5]
    report = {"ok": not errors, "segments": len(timeline), "issue_count": len(issues),
              "error_count": len(errors), "warning_count": len(warnings),
              "issue_types": by_type, "low_coverage_segments": low_coverage,
              "issues": issues}
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"boundary audit: {len(timeline)} segments, {len(errors)} errors, "
          f"{len(warnings)} warnings -> {args.report}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
