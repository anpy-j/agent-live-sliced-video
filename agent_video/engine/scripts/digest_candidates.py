# -*- coding: utf-8 -*-
"""Build a bounded, categorized candidate digest without model calls."""
import argparse
import bisect
import difflib
import json

# 常规片段下限；验收硬门槛是 1.2 秒。
MIN_SPEECH = 1.5
MAX_SPEECH = 5.0

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--other-share", type=float, default=0.25,
                        help="未分类候选最多占摘要的比例（它们多为直播碎片）")
    parser.add_argument("--near-duplicate", type=float, default=0.90)
    parser.add_argument("--max-total-chars", type=int, default=6000,
                        help="候选正文总字符预算；条数上限不能单独约束模型上下文")
    parser.add_argument("--compact", action="store_true",
                        help="输出供模型读取的短键紧凑 JSON")
    args = parser.parse_args()
    rows = json.load(open(args.input, encoding="utf-8"))
    # 只把可直接进入编排的剪辑原子暴露给 AI。过短片段无法可靠对齐，过长片段
    # 即使被选中也会在后续门禁失败；在候选阶段剔除比渲染前才报错更省时间。
    rows = [row for row in rows if MIN_SPEECH <=
            float(row.get("end", 0)) - float(row.get("start", 0)) <= MAX_SPEECH]
    ranked = sorted(rows, key=lambda row: (-quality(row), float(row.get("start", 0))))
    kept, norms = [], []
    for row in ranked:
        norm = normalize(row.get("text", ""))
        if not norm or any(norm == old or (min(len(norm), len(old)) >= 8 and
                           difflib.SequenceMatcher(None, norm, old).ratio() >= args.near_duplicate)
                           for old in norms):
            continue
        item = dict(row)
        item["category"] = category(item.get("text", ""))
        item["quality_hint"] = quality(item)
        kept.append(item)
        norms.append(norm)
    buckets = {name: [] for name in list(CATEGORIES) + ["other"]}
    for row in kept:
        buckets[row["category"]].append(row)
    named = [name for name in CATEGORIES]
    digest = []
    chars = 0
    while any(buckets[name] for name in named) and len(digest) < args.limit:
        for name in named:
            if buckets[name] and len(digest) < args.limit:
                item = buckets[name].pop(0)
                size = len(item.get("text", ""))
                if chars + size <= args.max_total_chars:
                    digest.append(item)
                    chars += size
    if len(digest) < args.limit and buckets["other"]:
        for item in buckets["other"][:max(0, min(args.limit - len(digest),
                                                 int(args.limit * args.other_share)))]:
            size = len(item.get("text", ""))
            if chars + size > args.max_total_chars:
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
                    "c": row.get("category", "other"), "t": row.get("text", "")}
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
