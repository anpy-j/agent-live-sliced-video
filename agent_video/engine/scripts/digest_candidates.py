# -*- coding: utf-8 -*-
"""Build a bounded, categorized candidate digest without model calls."""
import argparse
import bisect
import difflib
import json
import re

try:
    from .dependency_graph import resolve_dependency_closure
    from .textnorm import (content_rejection, context_dependent_start,
                           incomplete_ending)
except ImportError:  # Script entry point: its directory is already on sys.path.
    from dependency_graph import resolve_dependency_closure
    from textnorm import (content_rejection, context_dependent_start,
                          incomplete_ending)

# 常规片段下限；验收硬门槛是 1.2 秒。
MIN_SPEECH = 1.5
# 合并目标：切片由 2-5 秒的声音单元组成，故短句只向上合并到对齐余量后的 5 秒上限。
# 旧值 18 秒会把若干短句粘成一个长段，直接导致「90 秒只有 10 段」的编排结果。
TARGET_SPEECH = 4.7
MAX_SPEECH = 5.0
# 硬上限：只留给原生就不可切分的完整长句；不再允许把短句合并到这个长度。
MAX_COMPLETE_SPEECH = 8.0

LEADING_REFERENCE_RE = re.compile(
    r"^(?:(?:这|那)(?:个|种|样|条|套|双|件|款)|这些|那些|它|它们|"
    r"穿上以后|腰部这里|它整个就是|你看这个)"
)

CATEGORIES = {
    "hook": ("一定", "千万", "注意", "告诉你", "说实话", "真的",
             "别急", "先别", "记住", "划重点", "必须", "我跟你说"),
    "result": ("显瘦", "显高", "比例", "腰线", "气质", "好看", "上身", "腰臀比",
               "收腹", "时装感", "显腿长", "显腰", "修身", "藏肉"),
    "pain": ("胯宽", "腰腹", "肚子", "显胖", "臃肿", "不会搭", "挑身材", "买不到",
             "穿不了", "遮肉", "拜拜肉", "肩宽", "背厚", "卡码", "撑不起来",
             "偏瘦", "烦恼", "不知道", "很难"),
    "material": ("面料", "材质", "手感", "光泽", "哑光", "纹理", "厚度", "透气",
                 "醋酸", "羊毛", "里衬", "里称", "内衬", "真丝", "弹力", "保暖",
                 "不热", "不闷", "亲肤", "垂感", "百分百", "含量", "闷汗"),
    "craft": ("工艺", "走线", "剪裁", "拼接", "定制", "结构", "收腰", "版型",
              "迪奥", "dior", "打版", "师傅", "工厂", "工序", "做工", "车工",
              "定型", "廓形", "经典"),
    "color": ("颜色", "黑色", "白色", "米色", "灰色", "咖色", "藏青", "杏色",
              "卡其", "两色", "配色", "驼色", "燕麦", "色系"),
    "styling": ("搭配", "配裤子", "配裙子", "内搭", "叠穿", "穿法", "单穿",
                "裸穿", "打底", "衬衫", "牛仔裤", "阔腿", "半裙", "里面穿",
                "外面穿", "怎么搭"),
    "fit": ("身高", "体重", "尺码", "身材", "小个子", "微胖", "斤", "xs", "xl",
            "最大", "一百五十", "能穿", "适合", "宽松"),
    "scene": ("上班", "通勤", "约会", "聚会", "旅游", "场合", "场景", "出差",
              "逛街", "度假", "年会", "日常", "出门", "空调", "室内", "春天",
              "夏天", "秋天", "冬天", "四季", "季节"),
    "proof": ("销量", "复购", "回购", "反馈", "好评", "订单", "专柜", "品牌",
              "升级", "四年", "每一年", "价格", "块钱", "性价比", "质价比",
              "几万", "老客", "新粉", "当年"),
    "close": ("值得", "推荐", "必备", "实穿", "利用率", "一件多穿", "闭眼",
              "入手", "不要错过", "上架", "库存", "最后", "感谢", "关注"),
    "personality": ("我觉得", "我认为", "我自己", "我不喜欢", "我的审美", "说句实话",
                    "我宁愿", "我一直", "我个人", "我穿衣服", "在我看来"),
    "story": ("以前", "有一次", "当时", "后来", "结果", "刚开始", "没想到", "之前",
              "那时候", "第一次", "最后发现"),
    "reaction": ("哇", "天哪", "太好看了", "绝了", "惊喜", "笑死", "居然", "真的绝",
                 "你们看", "看到了吗"),
    "visual": ("看这里", "转一圈", "看侧面", "看背面", "走两步", "拉一下", "看细节",
               "上身看", "看长度", "看腰线", "看袖子", "看领口"),
}

CONTEXT_STARTS = ("然后", "所以", "但是", "不过", "而且", "还有", "因为", "这个的话",
                  "它的话", "那这个")
LOW_INFORMATION = ("姐妹们", "宝贝们", "宝宝们", "有没有", "能听到吗", "看得到吗",
                   "欢迎来到", "点点关注")


def normalize(text):
    return "".join(c.lower() for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def category(text):
    scored = [(sum(text.count(word) for word in words), name)
              for name, words in CATEGORIES.items()]
    score, name = max(scored)
    return name if score else "other"


def quality(row):
    text = row.get("text", "")
    duration = float(row.get("end", 0)) - float(row.get("start", 0))
    length = len(normalize(text))
    score = 0
    score += 3 if 1.2 <= duration <= 6.5 else 0
    score += 3 if 8 <= length <= 42 else 0
    score += 2 if text[-1:] in "。！？!?" else 0
    score += sum(word in text for words in CATEGORIES.values() for word in words)
    score += 2 if any(word in text for word in CATEGORIES["personality"] +
                      CATEGORIES["story"] + CATEGORIES["reaction"] + CATEGORIES["visual"]) else 0
    stripped = text.strip(" ，,。！？!?；;：:")
    if stripped.startswith(CONTEXT_STARTS):
        score -= 2
    if any(stripped.startswith(word) for word in LOW_INFORMATION) and length < 18:
        score -= 5
    if stripped.endswith(("因为", "所以", "但是", "而且", "然后", "如果", "比如")):
        score -= 4
    return score


def dependency_metadata(rows):
    """Attach stable speech-unit identity and immediate source context.

    Candidate text is never rewritten.  Context fields are review-only and dependency
    flags make it impossible for a planner to silently promote a fragment to a
    standalone clip.
    """
    result = []
    for index, source in enumerate(rows):
        row = dict(source)
        text = str(row.get("text") or "").strip()
        previous = rows[index - 1] if index else None
        following = rows[index + 1] if index + 1 < len(rows) else None
        requires_previous = bool(context_dependent_start(text) or
                                 LEADING_REFERENCE_RE.match(text))
        requires_next = bool(incomplete_ending(text))
        utterance_id = str(row.get("utterance_id") or f"utterance-{index:06d}")
        duration = float(row.get("end", 0)) - float(row.get("start", 0))
        is_long = duration > MAX_SPEECH
        row.update({
            "utterance_id": utterance_id,
            "atom_id": str(row.get("atom_id") or f"{utterance_id}:0"),
            "previous_text": str((previous or {}).get("text") or ""),
            "current_text": text,
            "next_text": str((following or {}).get("text") or ""),
            "requires_previous": requires_previous,
            "requires_next": requires_next,
            "safe_standalone": not (requires_previous or requires_next),
            "required_atom_ids": ([str((previous or {}).get("atom_id") or
                                        f"utterance-{index - 1:06d}:0")]
                                  if requires_previous and previous else []) +
                                 ([str((following or {}).get("atom_id") or
                                        f"utterance-{index + 1:06d}:0")]
                                  if requires_next and following else []),
        })
        if is_long:
            row["long_complete_utterance"] = True
        result.append(row)
    return result


def dependency_aware_deduplicate(rows, threshold=0.90):
    """Deduplicate complete candidate groups without orphaning a live dependant."""
    rows = [dict(row, _graph_id=index) for index, row in enumerate(rows)]
    referenced = {str(atom) for row in rows
                  for atom in (row.get("required_atom_ids") or [])}
    selected: list[dict] = []
    norms: list[str] = []
    for row in sorted(rows, key=lambda item: (-quality(item), float(item.get("start", 0)))):
        current = normalize(row.get("text", ""))
        if not current:
            continue
        duplicate = next((index for index, old in enumerate(norms)
                          if current == old or (min(len(current), len(old)) >= 8 and
                          difflib.SequenceMatcher(None, current, old).ratio() >= threshold)), None)
        if duplicate is None:
            selected.append(row)
            norms.append(current)
            continue
        old = selected[duplicate]
        current_protected = str(row.get("atom_id")) in referenced
        old_protected = str(old.get("atom_id")) in referenced
        # If both source atoms are live dependencies, they are not interchangeable:
        # keep both exact neighbours.  If only one is referenced, it wins even when
        # the other isolated sentence has a slightly better keyword score.
        if current_protected and old_protected:
            selected.append(row)
            norms.append(current)
        elif current_protected and not old_protected:
            selected[duplicate] = row
            norms[duplicate] = current
    selected_ids = {int(row["_graph_id"]) for row in selected}
    resolution = resolve_dependency_closure(rows, selected_ids, id_key="_graph_id")
    result = [row for row in rows if int(row["_graph_id"]) in resolution.valid_ids]
    for row in result:
        row.pop("_graph_id", None)
    return result


def should_merge_speech_units(left: dict, right: dict,
                              min_duration: float = 1.5,
                              max_duration: float = 18.0,
                              max_gap: float = 0.40) -> bool:
    gap = float(right.get("start", 0)) - float(left.get("end", 0))
    total_dur = float(right.get("end", 0)) - float(left.get("start", 0))
    if gap < -0.05 or gap > max_gap or total_dur > max_duration:
        return False
    dur_left = float(left.get("end", 0)) - float(left.get("start", 0))
    dur_right = float(right.get("end", 0)) - float(right.get("start", 0))
    text_left = str(left.get("text") or "").strip()
    text_right = str(right.get("text") or "").strip()

    rej_left = content_rejection(text_left)
    rej_right = content_rejection(text_right)
    if rej_left not in {None, "context_dependent_start", "incomplete_sentence"}:
        return False
    if rej_right not in {None, "context_dependent_start", "incomplete_sentence"}:
        return False

    if dur_left < min_duration or dur_right < min_duration:
        return True
    if incomplete_ending(text_left) is not None:
        return True
    if context_dependent_start(text_right) is not None or LEADING_REFERENCE_RE.match(text_right):
        return True
    if text_left[-1:] not in "。！？!?" and total_dur <= 10.0 and gap <= 0.25:
        return True
    return False


def merge_text_parts(text_a: str, text_b: str) -> str:
    text_a = text_a.rstrip(" ，,")
    text_b = text_b.lstrip(" ，,")
    if not text_a:
        return text_b
    if not text_b:
        return text_a
    if text_a[-1] in "，,。！？!?；;：:" or text_b[0] in "，,。！？!?；;：:":
        return text_a + text_b
    return text_a + "，" + text_b


def merge_short_units(rows: list[dict], min_duration: float = 1.5,
                      max_duration: float = 18.0, max_gap: float = 0.40) -> list[dict]:
    """Merge short fragments (<1.5s), dependent clauses, and continuations into natural speech units."""
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda item: float(item.get("start", 0)))
    merged: list[dict] = []
    for item in sorted_rows:
        row = dict(item)
        if not merged:
            merged.append(row)
            continue
        prev = merged[-1]
        if should_merge_speech_units(prev, row, min_duration, max_duration, max_gap):
            prev["end"] = max(float(prev.get("end", 0)), float(row.get("end", 0)))
            prev["text"] = merge_text_parts(str(prev.get("text") or ""), str(row.get("text") or ""))
            if row.get("review_terms"):
                existing = prev.get("review_terms") or []
                prev["review_terms"] = sorted(set(existing) | set(row["review_terms"]))
        else:
            merged.append(row)

    result: list[dict] = []
    for row in merged:
        if result:
            prev = result[-1]
            gap = float(row.get("start", 0)) - float(prev.get("end", 0))
            total_dur = float(row.get("end", 0)) - float(prev.get("start", 0))
            dur_row = float(row.get("end", 0)) - float(row.get("start", 0))
            if (dur_row < min_duration and -0.05 <= gap <= max_gap and total_dur <= max_duration
                    and content_rejection(str(row.get("text") or "")) in {None, "context_dependent_start", "incomplete_sentence"}
                    and content_rejection(str(prev.get("text") or "")) in {None, "context_dependent_start", "incomplete_sentence"}):
                prev["end"] = max(float(prev.get("end", 0)), float(row.get("end", 0)))
                prev["text"] = merge_text_parts(str(prev.get("text") or ""), str(row.get("text") or ""))
                continue
        result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--limit", type=int, default=0,
                        help="候选条数上限；0 表示不限制。脚本只做确定性筛选，"
                             "语义可用性由 AI 逐条审核，不应在这里提前淘汰候选")
    parser.add_argument("--other-share", type=float, default=0.25,
                        help="未分类候选最多占摘要的比例（仅在 limit>0 时生效）")
    parser.add_argument("--near-duplicate", type=float, default=0.90)
    parser.add_argument("--max-total-chars", type=int, default=0,
                        help="候选正文总字符预算；0 表示不限制（仅在 limit>0 时生效，"
                             "分批审核后单批上下文由调用方控制）")
    parser.add_argument("--compact", action="store_true",
                        help="输出供模型读取的短键紧凑 JSON")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        rows = json.load(handle)
    # 前置短句智能合并：依据间隙 (<0.4s)、停顿、标点和语义依赖，将 <1.5s 的
    # 短促转折或修饰句前置合并为 1.5-4.7 秒的自然声音单元，避免 60% 可用上下文在
    # 筛选前被简单物理丢弃。上限对齐 5 秒片段门禁，使切片天然由多段短单元组成。
    rows = merge_short_units(rows, min_duration=MIN_SPEECH, max_duration=TARGET_SPEECH, max_gap=0.40)
    rows = dependency_metadata(rows)
    rows = [row for row in rows if 1.2 <=
            float(row.get("end", 0)) - float(row.get("start", 0)) <= MAX_COMPLETE_SPEECH
            and content_rejection(str(row.get("text", ""))) in
            {None, "context_dependent_start", "incomplete_sentence"}]
    kept = dependency_aware_deduplicate(rows, args.near_duplicate)
    for row in kept:
        item = dict(row)
        item["category"] = category(item.get("text", ""))
        item["quality_hint"] = quality(item)
        row.update(item)
    buckets = {name: [] for name in list(CATEGORIES) + ["other"]}
    for row in kept:
        buckets[row["category"]].append(row)
    named = [name for name in CATEGORIES]
    if args.limit <= 0:
        # 不做任何基于关键词/评分的取舍：脚本只保留确定性筛选（时长、垃圾规则、
        # 完全重复）之后的全部候选，交给 AI 分批逐条判定可用性。
        digest = kept
        chars = sum(len(row.get("text", "")) for row in digest)
    else:
        digest = []
        chars = 0
        while any(buckets[name] for name in named) and len(digest) < args.limit:
            for name in named:
                if buckets[name] and len(digest) < args.limit:
                    item = buckets[name].pop(0)
                    size = len(item.get("text", ""))
                    if args.max_total_chars <= 0 or chars + size <= args.max_total_chars:
                        digest.append(item)
                        chars += size
        if len(digest) < args.limit and buckets["other"]:
            for item in buckets["other"][:max(0, min(args.limit - len(digest),
                                                     int(args.limit * args.other_share)))]:
                size = len(item.get("text", ""))
                if args.max_total_chars > 0 and chars + size > args.max_total_chars:
                    break
                digest.append(item)
                chars += size
    digest.sort(key=lambda row: float(row.get("start", 0)))
    payload = digest
    if args.compact:
        payload = []
        for index, row in enumerate(digest):
            item = {"i": index, "s": round(float(row.get("start", 0)), 3),
                    "e": round(float(row.get("end", 0)), 3),
                    "c": row.get("category", "other"), "t": row.get("text", ""),
                    "q": int(row.get("quality_hint", 0)),
                    "utterance_id": row["utterance_id"],
                    "atom_id": row["atom_id"],
                    "previous_text": row["previous_text"],
                    "current_text": row["current_text"],
                    "next_text": row["next_text"],
                    "requires_previous": row["requires_previous"],
                    "requires_next": row["requires_next"],
                    "safe_standalone": row["safe_standalone"],
                    "required_atom_ids": row["required_atom_ids"]}
            if row.get("long_complete_utterance"):
                item["long_complete_utterance"] = True
            if row.get("review_terms"):
                item["r"] = row["review_terms"]
            payload.append(item)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False,
                  separators=(",", ":") if args.compact else None,
                  indent=None if args.compact else 1)
    counts = {name: sum(row["category"] == name for row in digest)
              for name in list(CATEGORIES) + ["other"]}
    print(f"candidate digest: {len(rows)} -> {len(digest)} / {chars} chars; "
          f"categories={counts} -> {args.output}")


if __name__ == "__main__":
    main()
