# -*- coding: utf-8 -*-
"""女装直播领域词表：把 whisper 的同音/近音误听改回商品术语。

字幕的自动校验一直是「文本和音频是否一致」，而且按拼音比对（cuts.py 的 syl()），
所以同音误听既能骗过对齐、又会原样写进成片字幕——观众看到的就是「裸口」「长蹄」
「安伦」。这一层只做确定性替换：命中即改写，未命中不动。

原则：
  - 只收本领域里写法唯一的词，含糊的不碰。「真皮大底」的「大底」是对的，所以只
    替换带商品语境的「大底T恤」，绝不替换单个「大底」。
  - 替换幂等：正确写法不会再被匹配到。
  - 词表变化要让 ASR 缓存失效，否则旧转写会被复用（prep.vocab_identity）。
"""
import re


TERM_FIXES = [
    (r"大底(?=[Tt]恤)", "大版"),
    (r"一件提取", "一件起批"),
    (r"裸口", "罗纹"),
    (r"弹粒", "弹力"),
    (r"安伦", "氨纶"),
    (r"背信", "背心"),
    (r"长蹄", "长T"),
    (r"俄容", "鹅绒"),
    (r"秀底", "袖底"),
    (r"航风", "行缝"),
    (r"先货", "现货"),
]

_COMPILED = [(re.compile(pattern), replacement) for pattern, replacement in TERM_FIXES]


def correct_terms(text):
    """把已知的同音误听改回正确术语；不改动未命中的文本。"""
    value = text or ""
    for pattern, replacement in _COMPILED:
        value = pattern.sub(replacement, value)
    return value
