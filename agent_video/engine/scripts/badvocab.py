# -*- coding: utf-8 -*-
"""本地合规词表（唯一合规词源）。

分两层：

- **硬禁**：命中即整句淘汰，不进入候选、不进入成片。
- **复核**：只标记，让调用方核对时效与上下文后决定。

基础词表是通用默认。账号级差异（例如把「价格/现货/发货/快飞」也当硬禁）
不改代码，用词表配置 JSON 覆盖：

    DOUYIN_VOCAB_PROFILE=<project>/agent_video/engine/profiles/douyin-strict.json

配置键（全部可选）：

    hard_add / hard_remove          增删硬禁词
    review_add / review_remove      增删复核词
    hard_regex_add / hard_regex_remove
    review_regex_add / review_regex_remove
    secondary_products              副商品词表（正则片段，供 audit_bounds 用）
    secondary_attributes            副商品属性词表（正则片段）

硬禁优先：同一个词同时出现在两层时，一律按硬禁处理。
`run_slice.py` 默认使用随包提供的 `profiles/douyin-strict.json` 并把它写进子进程环境；
直接调用底层脚本时不带配置，只是通用默认。
"""
import json
import os
import re

HARD_PLAIN = [
    # 链接及引流
    "上链接", "放链接", "开链接", "挂链接", "一号链接", "链接上车", "小黄车",
    "上车", "链接", "宝子们拍", "点击下方", "左下方",
    # 直播状态
    "直播间", "开播", "下播", "直播", "场控",
    # 销售结果与强催单
    "卖出去", "卖爆", "卖空", "售罄", "爆单", "卖了多少单",
    "快拍", "拍它", "拍一单", "加单", "上单", "单量", "秒杀", "抢购",
    # 任何形式的报价：专柜价也是价格
    "售价", "专柜价", "专柜售价", "原价", "划线价", "标价", "报价",
]

REVIEW_PLAIN = [
    # 库存、履约与价格：可能有效，但要核对时效和上下文
    "下单", "拍下", "发货", "现货", "库存", "快飞", "价格", "到手价", "优惠价", "福利价",
    "打折", "折扣", "降价", "多少钱", "限量", "专柜",
    # 第三方品牌：只在涉及仿冒、攀附或无法证实的对比时剔除
    "迪奥", "dior", "香奈儿", "chanel", "爱马仕", "hermes", "路易威登", "lv",
    "古驰", "gucci", "普拉达", "prada", "思琳", "celine", "博柏利", "burberry",
    "芬迪", "fendi", "阿玛尼", "armani", "max mara", "bottega", "bv",
    "迪家", "小香", "驴牌",
]

HARD_REGEX = [
    r"\d+\s*单",          # 300单 / 上100单
    r"[一二三四五六七八九十百千]+\s*单",
    r"\d{1,3}(?:,\d{3})+",  # 逗号千分位数字几乎都是报价，例如 5,980专柜售价
]
REVIEW_REGEX = [r"多少\s*钱", r"多钱", r"几\s*折"]

# 副商品判定的默认词表（女装场景）。换类目时用配置覆盖。
SECONDARY_PRODUCTS = "牛仔裤|裤子|半裙|裙子|外套|衬衫|打底(?:衫)?|内搭|鞋子|包包"
SECONDARY_ATTRIBUTES = "中腰|低腰|矮腰|高腰|裤长|弹力|尺码|版型|面料|材质|颜色"

DEFAULT_PROFILE_NAME = "douyin-strict.json"


def default_profile_path():
    """随包提供的账号级词表；run_slice 默认使用它。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "profiles", DEFAULT_PROFILE_NAME)
    return path if os.path.isfile(path) else None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value if str(x).strip()]


def _dedup(values):
    seen, out = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def load_profile(path=None):
    """读入一份词表配置；文件不存在或不可解析时返回空配置（不抛异常）。"""
    path = path or os.environ.get("DOUYIN_VOCAB_PROFILE")
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base, add, remove):
    out = [x for x in base if x not in set(remove)]
    out.extend(x for x in add if x not in out)
    return _dedup(out)


def _build(profile):
    hard = _merge(HARD_PLAIN, _as_list(profile.get("hard_add")),
                  set(_as_list(profile.get("hard_remove"))))
    review = _merge(REVIEW_PLAIN, _as_list(profile.get("review_add")),
                    set(_as_list(profile.get("review_remove"))))
    hard_re = _merge(HARD_REGEX, _as_list(profile.get("hard_regex_add")),
                     set(_as_list(profile.get("hard_regex_remove"))))
    review_re = _merge(REVIEW_REGEX, _as_list(profile.get("review_regex_add")),
                       set(_as_list(profile.get("review_regex_remove"))))
    # 硬禁优先：出现在硬禁里的词不再作为「复核」提示，避免同一条同时报两级。
    review = [x for x in review if x not in set(hard)]
    return hard, review, hard_re, review_re


PROFILE: dict = {}
HARD: list = []
REVIEW: list = []
HARD_RES: list = []
REVIEW_RES: list = []
BAD_RE: re.Pattern = re.compile(r"(?!x)x")
REVIEW_RE: re.Pattern = re.compile(r"(?!x)x")


def _refresh():
    """按当前 ``DOUYIN_VOCAB_PROFILE`` 重建全局词表与正则。"""
    global PROFILE, HARD, REVIEW, HARD_RES, REVIEW_RES, BAD_RE, REVIEW_RE
    global SECONDARY_PRODUCTS, SECONDARY_ATTRIBUTES
    PROFILE = load_profile()
    HARD, REVIEW, HARD_RES, REVIEW_RES = _build(PROFILE)
    SECONDARY_PRODUCTS = str(PROFILE.get("secondary_products") or SECONDARY_PRODUCTS)
    SECONDARY_ATTRIBUTES = str(PROFILE.get("secondary_attributes") or SECONDARY_ATTRIBUTES)
    BAD_RE = re.compile("|".join([re.escape(p) for p in HARD]
                                 + [f"(?:{p})" for p in HARD_RES]), re.I)
    REVIEW_RE = re.compile("|".join([re.escape(p) for p in REVIEW]
                                    + [f"(?:{p})" for p in REVIEW_RES]), re.I)


def reload_profile(path=None):
    """重新加载词表，供规则热更新用。

    ``path`` 非空时先写入 ``DOUYIN_VOCAB_PROFILE`` 环境变量再加载；
    调用方模块（如 ``filter``）必须通过 ``badvocab.BAD_RE`` 动态取用才会生效。
    """
    if path is not None:
        os.environ["DOUYIN_VOCAB_PROFILE"] = str(path)
    _refresh()
    return summary()


_refresh()


def hit(text):
    """返回命中的违禁词，无命中返回 None。"""
    m = BAD_RE.search(text or "")
    return m.group(0) if m else None


def review_hits(text):
    """返回所有需上下文复核的命中，去重且保留出现顺序。"""
    return list(dict.fromkeys(m.group(0) for m in REVIEW_RE.finditer(text or "")))


def summary():
    """词表生效状况，写进 pipeline 摘要供排查（只报数量与来源，不泄词表内容）。"""
    return {"profile": os.environ.get("DOUYIN_VOCAB_PROFILE") or None,
            "hard": len(HARD), "review": len(REVIEW),
            "hard_regex": len(HARD_RES), "review_regex": len(REVIEW_RES)}
