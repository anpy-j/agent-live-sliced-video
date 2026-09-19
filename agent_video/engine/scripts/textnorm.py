# -*- coding: utf-8 -*-
"""Shared text normalization and deterministic sentence-boundary rules.

字幕常写「150斤 / 90白鹅」，而 whisper 转写出「一百五十斤 / 九十白鹅」。
不归一就字符级对不上：对齐会被迫放宽，审计会把正确选段误报成
spoken_text_mismatch（实测相似度掉到 0.67–0.81，反复触发无意义的换段）。
cuts.py 与 audit_bounds.py 必须用同一份实现，否则两侧归一方式不一致。
"""
import re


INCOMPLETE_END_RE = re.compile(
    r"(?:因为|所以|但是|而且|然后|如果|虽然|不过|并且|以及|或者|要是|只要|除非|比如|包括|接着)$"
)
CONTEXT_DEPENDENT_START_RE = re.compile(
    r"^(?:的(?!确)|然后|所以|但是|不过|而且|还有|因为|其实这个|这个的话|它的话|那这个)"
)
STAGE_CHATTER_RE = re.compile(
    r"(?:删掉|先删|再拍(?:一个|一下)|稍等(?:一下)?|给我拿|拿出来|帮我置顶|"
    r"后台|催单|挂一个|上链接|报一下|还有多少(?:件|条|双)|48小时(?:可以)?飞|"
    r"发货|物流|运费险)"
)
MALFORMED_SPEECH_RE = re.compile(
    r"(?:羊毛手手|一定要把我告诉你|想给你做出这么漂(?:啊|呀|哎|哦|$))"
)
_D = "零一二三四五六七八九"


def _int2cn(n):
    if n < 10:
        return _D[n]
    if n < 20:
        return "十" + (_D[n % 10] if n % 10 else "")
    if n < 100:
        return _D[n // 10] + "十" + (_D[n % 10] if n % 10 else "")
    if n < 1000:
        s = _D[n // 100] + "百"
        r = n % 100
        if r == 0:
            return s
        return s + ("零" + _D[r] if r < 10 else _int2cn(r))
    return str(n)


def cn_num(t):
    out, i = [], 0
    t = t or ""
    while i < len(t):
        if t[i].isdigit():
            j = i
            while j < len(t) and t[j].isdigit():
                j += 1
            out.append(_int2cn(int(t[i:j])))
            i = j
        else:
            out.append(t[i])
            i += 1
    return "".join(out)


def incomplete_ending(text):
    """Return an obvious dangling connector, otherwise None.

    Punctuation alone cannot make a trailing conjunction semantically complete, so it is
    stripped before the check.  This intentionally handles only deterministic cases; less
    certain semantic judgments remain outside the platform's automatic claims.
    """
    value = re.sub(r"[\s。！？!?…，,；;：:]+$", "", text or "")
    match = INCOMPLETE_END_RE.search(value)
    return match.group(0) if match else None


def context_dependent_start(text):
    """Return an obvious context-dependent opening connector, otherwise None."""
    value = re.sub(r"^[\s。！？!?…，,；;：:]+", "", text or "")
    match = CONTEXT_DEPENDENT_START_RE.search(value)
    return match.group(0) if match else None


def content_rejection(text):
    """Return a high-confidence reason why a clip is not publishable speech.

    Keep this deterministic and deliberately conservative.  Ambiguous selling quality
    remains the planner's job, while obvious dependent fragments, production chatter,
    and known malformed ASR must never be used by automatic duration filling.
    """
    value = re.sub(r"\s+", "", text or "")
    if not value:
        return "empty_text"
    opening = context_dependent_start(value)
    if opening:
        return "context_dependent_start"
    ending = incomplete_ending(value)
    if ending:
        return "incomplete_sentence"
    if STAGE_CHATTER_RE.search(value):
        return "stage_chatter"
    if MALFORMED_SPEECH_RE.search(value):
        return "malformed_speech"
    return None
