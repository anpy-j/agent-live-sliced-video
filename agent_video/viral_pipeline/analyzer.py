from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any


ANALYZER_VERSION = "viral-dna-v1"

HOOK_PATTERNS = {
    "夸张感受": ("绝了", "疯了", "后悔", "没想到", "太", "真的"),
    "结果前置": ("显瘦", "显高", "高级", "气质", "效果", "上身"),
    "身份共鸣": ("姐妹", "小个子", "微胖", "胯宽", "肩宽", "黄皮"),
    "反差悬念": ("但是", "居然", "看着", "实际上", "结果", "反而"),
}

TOPIC_PATTERNS = {
    "上身效果": ("上身", "显瘦", "显高", "气质", "身材", "腿长", "比例"),
    "穿搭": ("搭配", "内搭", "外搭", "裤子", "裙子", "鞋", "通勤", "场合"),
    "面料质感": ("面料", "羊毛", "羊绒", "成分", "手感", "柔软", "质感"),
    "版型剪裁": ("版型", "剪裁", "腰线", "肩线", "领口", "袖子", "长度"),
    "情绪体验": ("喜欢", "惊喜", "舒服", "爱了", "感觉", "后悔", "值"),
    "价格价值": ("价格", "便宜", "贵", "划算", "性价比", "块钱"),
}


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？!?；;\n]+", text) if part.strip()]


def _best_label(text: str, patterns: dict[str, tuple[str, ...]], default: str) -> str:
    scored = [(sum(text.count(word) for word in words), label)
              for label, words in patterns.items()]
    score, label = max(scored, default=(0, default))
    return label if score else default


def analyze_reference_locally(reference: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic baseline DNA without consuming AI tokens.

    The result is deliberately versioned. A later AI analysis can replace it
    without changing the reference record or the V2 database schema.
    """
    transcript = str(reference.get("transcript") or "").strip()
    sentences = _sentences(transcript)
    if not sentences:
        raise ValueError("爆款文本不能为空")
    first = sentences[0]
    counts = Counter()
    for label, words in TOPIC_PATTERNS.items():
        counts[label] = sum(transcript.count(word) for word in words)
    total = sum(counts.values()) or 1
    ratios = {label: round(count / total, 3) for label, count in counts.items() if count}
    primary = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] else "综合表达"
    duration = float(reference.get("duration_seconds") or 0)
    average_seconds = round(duration / len(sentences), 2) if duration else None
    return {
        "version": ANALYZER_VERSION,
        "source_hash": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        "hook": {
            "mechanism": _best_label(first, HOOK_PATTERNS, "直接陈述"),
            "mentions_product": any(word in first for word in
                                    ("衣", "裤", "裙", "衫", "外套", "大衣", "毛衣")),
            "text": first[:160],
        },
        "primary_focus": primary,
        "content_distribution": ratios,
        "narrative_units": [
            {"position": index, "label": _best_label(sentence, TOPIC_PATTERNS, "其他"),
             "text": sentence[:180]}
            for index, sentence in enumerate(sentences)
        ],
        "rhythm": {
            "sentence_count": len(sentences),
            "average_chars": round(sum(len(item) for item in sentences) / len(sentences), 1),
            "average_unit_seconds": average_seconds,
        },
        "language_style": {
            "question_count": transcript.count("？") + transcript.count("?"),
            "exclamation_count": transcript.count("！") + transcript.count("!"),
            "colloquial_markers": sum(transcript.count(word) for word in
                                      ("真的", "就是", "你看", "姐妹", "我跟你说")),
        },
    }
