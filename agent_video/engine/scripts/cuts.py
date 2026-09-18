# -*- coding: utf-8 -*-
"""切点定稿：逐字时间戳对齐 + 静音核兜底 + 冲突自动收敛（不提问）。

用法:
  python cuts.py <audio16k.wav> <picks.json> <out_timeline.json>
                 [--words PATH] [--sentences PATH] [--model PATH] [--maxblk 3.5] [--tmp DIR]

picks.json:
  {"offsets": {"1": [0.0, "src1.mp4"]},
   "words":   {"1": "src1_slice/words.json"},     # 多源逐源给；单源可用 --words
   "picks":   [{"src": 1, "start": 12.3, "end": 15.8, "text": "完整一句话"}]}
  start/end 为拼接音轨上的全局时间码（与 audio16k.wav 对齐）。

三种结局全部自动决定，不向用户提问：
  char/snap  对齐成功           -> 直接用，need_manual=false
  relaxed    边界不可信         -> 放宽余量（首 0.4s / 尾 0.6s），need_manual=true
  skipped    归属存疑/带入违禁词 -> 整句淘汰，不进成片
"""
import argparse
import array
import hashlib
import json
import math
import os
import subprocess
import sys
import wave

WIN, TH, MINPAUSE, CORE, FLANK = 0.02, 2200.0, 0.16, 1500.0, 3000.0
PAD_HEAD, PAD_TAIL = 0.4, 0.6

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from badvocab import BAD_RE  # noqa: E402
from asr_backend import Transcriber  # noqa: E402
from textnorm import cn_num  # noqa: E402

try:
    from zhconv import convert as _zh
except Exception:                                    # 缺库时退化为直通，不外抛
    def _zh(t, *a):
        return t

PROMPT_ZH = "以下是普通话口播，请使用简体中文，带标点。"


def simp(t):
    """繁简归一：局部窗口转写常出繁体，简体字表会对不上（实测踩过）。"""
    return _zh(t or "", "zh-cn")


def norm(t):
    t = cn_num(simp(t)).lower()
    return "".join(c for c in t
                   if c.isalnum() or "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")


try:
    from pypinyin import pinyin as _py, Style as _PSTYLE
except Exception:
    _py, _PSTYLE = None, None

_SYL = {}


def syl(c):
    """单字 -> 无声调拼音。ASR 错认几乎全是同音字（俄容/鹅绒、秀底/袖底、
    航风/行缝、做得很泡/做的很抛），按字形匹配会被一个错字打断，
    按拼音匹配才不会（实测某段覆盖率 0.07 → 改用拼音后应回到 0.9+）。"""
    if c in _SYL:
        return _SYL[c]
    if c.isascii():
        s = c.lower()
    elif _py is None:
        s = c
    else:
        r = _py(c, style=_PSTYLE.NORMAL, errors="ignore")
        s = r[0][0] if r and r[0] and r[0][0] else c
    _SYL[c] = s
    return s


def to_syl(t):
    t = cn_num(simp(t)).lower()
    return [syl(c) for c in t
            if c.isalnum() or "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf"]


def expand(words, base=0.0):
    """whisper 中文 token 是 1~4 字：按字数把时长均分到字；
    顺带繁转简、数字归一，并预存拼音与所属 token 的结束时间。
    te（token 结束）用于把切点吸附到 token 边界，避免切在半个 token 里。"""
    out = []
    for w in words:
        ch = cn_num(simp((w.get("w") or "").strip())).lower()
        if not ch:
            continue
        n = len(ch)
        ts, te = w["s"] + base, w["e"] + base
        for k, c in enumerate(ch):
            if not (c.isalnum() or "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf"):
                continue
            # 字幕只保证整块起止时间；所有字符共享块边界，强制切点落在字幕块外。
            boundary_only = bool(w.get("boundary_only"))
            out.append({"s": ts if boundary_only else
                        w["s"] + (w["e"] - w["s"]) * k / n + base,
                        "e": te if boundary_only else
                        w["s"] + (w["e"] - w["s"]) * (k + 1) / n + base,
                        "ts": ts, "te": te, "token": ch, "w": c, "y": syl(c)})
    return out


def load_audio(path):
    w = wave.open(path, "rb")
    sr, n = w.getframerate(), w.getnframes()
    d = array.array("h")
    d.frombytes(w.readframes(n))
    w.close()
    return sr, d


def _cores(rms, th, core, flank, minpause):
    N = len(rms)

    def loud(k, wsec=0.25):
        a = max(0, k - int(wsec / WIN))
        b = min(N, k + int(wsec / WIN) + 1)
        return max(rms[a:b]) if b > a else 0.0

    cands, i = [], 0
    while i < N:
        if rms[i] < th:
            j = i
            while j + 1 < N and rms[j + 1] < th:
                j += 1
            seg = rms[i:j + 1]
            if (j - i + 1) * WIN >= minpause and min(seg) < core:
                k = i + min(range(len(seg)), key=lambda z: seg[z])
                if loud(k - 12) > flank and loud(k + 12) > flank:
                    cands.append(k * WIN + WIN / 2)
            i = j + 1
        else:
            i += 1
    return sorted(set(cands))


def silence_cores(d, sr):
    """固定阈值优先；有背景音乐时能量永不落底（软静音陷阱），
    退化为按分位数自适应取阈值再找一次，仍取不到就返回空走放宽路径。"""
    step = int(WIN * sr)
    rms = [math.sqrt(sum(float(x) * x for x in d[i:i + step]) / step)
           for i in range(0, len(d) - step + 1, step)]
    cores = _cores(rms, TH, CORE, FLANK, MINPAUSE)
    if cores:
        return cores
    s = sorted(rms)
    if not s:
        return []
    th = s[int(0.06 * len(s))]
    core = max(1.0, s[int(0.015 * len(s))])
    cores = _cores(rms, th, core, th * 2.0, 0.12)
    if cores:
        print(f"静音核: 固定阈值无命中 → 自适应阈值 th={th:.0f} core={core:.0f} "
              f"命中 {len(cores)} 个（背景音乐素材常见）")
    return cores


_TRANSCRIBER = None


def local_tokens(d, sr, t0, t1, backend, model_path, tmpdir, tag="w"):
    """只对片段窗口补一次局部词级转写（模型常驻复用）；返回窗口内相对时间的原始 token。"""
    global _TRANSCRIBER
    a = max(0, int(t0 * sr))
    b = min(len(d), int(t1 * sr))
    if b - a < sr:
        return []
    tmp = os.path.join(tmpdir, f"win_{tag}.wav")
    w = wave.open(tmp, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(d[a:b].tobytes())
    w.close()
    if _TRANSCRIBER is None:
        _TRANSCRIBER = Transcriber(backend, model_path)
    segs = _TRANSCRIBER.transcribe(tmp, language="zh", beam_size=5,
                                   word_timestamps=True, initial_prompt=PROMPT_ZH)
    ws = []
    for s in segs:
        for x in s.get("words", []):
            ch = (x.get("w") or "").strip()
            if ch:
                ws.append({"s": x["s"], "e": x["e"], "w": ch})
    return ws


def window_base(t0, sr):
    """窗口起点在采样点上的落点：缓存重放与首次转写必须用同一个基准，否则会差几毫秒。"""
    return (max(0, int(t0 * sr)) / sr) if sr else float(t0)


def local_words(d, sr, t0, t1, backend, model_path, tmpdir, tag="w"):
    return expand(local_tokens(d, sr, t0, t1, backend, model_path, tmpdir, tag),
                  window_base(t0, sr))


GRID, WIN_PAD, WIN_CAP = 2.0, 1.0, 8.0
# 局部窗口已知的幽灵文本：whisper 在音乐段/起音不干净处会稳定吐这些固定串，
# 甚至把 initial_prompt 原样吐回来。它们字数不少（实测固定 19 个 token），
# 按 token 数量或时长都抓不到，只能靠下面的标记与全片转写交叉校验。
HALLUCINATION_MARKERS = ("请不吝", "点赞订阅", "转发打赏", "支持明镜", "明镜与", "点栏目",
                         "谢谢观看", "字幕由", "字幕组", "请使用简体中文", "以下是普通话口播",
                         "请订阅", "打赏支持")
AGREEMENT_FLOOR = 0.5


def stable_window(t0, t1, sentences, grid=GRID, pad=WIN_PAD, cap=WIN_CAP):
    """把补转写窗口外扩到完整句子，再吸附到 grid 的整数倍，长度压回 cap。

    窗口要同时满足两件互相拉扯的事：

    1. **稳定**：窗口既是逐窗口缓存的键，也是转写漂移的源头。原实现按 picks 顺序
       聚类（相邻窗口间距 ≤1.5s 就合并），改动一段就可能把邻近窗口合并或拆开，
       于是每轮全部窗口重转写（实测 142s/轮），同一句话还会在不同轮次转出不同
       文本。吸附到「完整句子 + 固定网格」后，同一片区域在任何 picks 组合下都
       得到同一个窗口 —— 前提是窗口只由这一段自己决定，所以调用方不再做邻接合并。
    2. **短**：实测这条素材 8~10 秒窗口基本可靠，越长越容易整段崩（只吐十几个
       token，甚至把 initial_prompt 原样吐回来）。所以网格取整之后必须再压一次
       cap —— 只在取整前收是没用的，取整本身每边最多各加 grid。

    压回时从「离选中句更远」的一侧收，收到刚好还覆盖选中句为止。上限是软约束：
    选中句自己就比 cap 长时以覆盖选中句为准（否则窗口里没有足够的 token 可切）。
    """
    t0, t1 = float(t0), float(t1)
    lo, hi = t0 - pad, t1 + pad
    for q in sentences or []:
        if q["start"] <= lo < q["end"]:
            lo = q["start"]
        if q["start"] < hi <= q["end"]:
            hi = q["end"]
    lo = math.floor(max(0.0, lo) / grid) * grid
    hi = math.ceil(hi / grid) * grid
    while hi - lo > cap + 1e-9:
        left, right = t0 - lo, hi - t1
        if left > right + 1e-9 and lo + grid <= t0 + 1e-9:
            lo += grid
        elif hi - grid >= t1 - 1e-9:
            hi -= grid
        else:
            break
    return round(lo, 3), round(hi, 3)


# 窗口落点的重试阶梯（单位：grid）。幽灵文本只跟落点相关，同一起点换终点仍然崩
# （实测 64-72/64-74/64-76 全崩），换起点就正常，所以平移窗口比调整长度有效。
# 全部候选都必须覆盖选中句，否则对齐时拿不到句首/句尾的 token。
WINDOW_STEPS = ((0, 0), (-1, 0), (0, 1), (1, 0), (-1, 1), (0, -1), (-2, 0), (0, 2))


def window_candidates(t0, t1, sentences, grid=GRID):
    """按 WINDOW_STEPS 生成一组「都覆盖选中句」的候选窗口，首个为稳定窗口。

    长度上限是**软约束**：选中句本身就长过 cap 时（模型偶尔会交上来一段 10 秒以上
    的选段），仍然必须给出覆盖它的窗口。原实现一律按 WIN_CAP 过滤，超长选段会把
    候选清空，`resolve_window()` 随后在 `best` 为 None 时解包而 TypeError 崩溃
    （实测 12s 选段只剩 1 个候选、13s 剩 0 个，报错落在 step=align 上，看起来像
    素材问题）。上限改成「至少能装下选中句」，超长选段退化成一次转写、由 relaxed
    兜底并标记 need_manual，不再崩。
    """
    lo, hi = stable_window(t0, t1, sentences, grid)
    limit = max(WIN_CAP + 2 * grid, (float(t1) - float(t0)) + 2 * grid)
    out, seen = [], set()
    for left, right in WINDOW_STEPS:
        c_lo, c_hi = round(lo + left * grid, 3), round(hi + right * grid, 3)
        if c_lo < 0 or c_hi - c_lo > limit + 1e-9:
            continue
        if not (c_lo <= t0 + 1e-9 and t1 <= c_hi + 1e-9):
            continue
        if (c_lo, c_hi) in seen:
            continue
        seen.add((c_lo, c_hi))
        out.append((c_lo, c_hi))
    return out


def text_overlap(left, right):
    """字符多重集重叠率：left 有多少比例的字符能在 right 里按出现次数找到。"""
    if not left:
        return 0.0
    counts = {}
    for char in right:
        counts[char] = counts.get(char, 0) + 1
    hit = 0
    for char in left:
        if counts.get(char, 0) > 0:
            counts[char] -= 1
            hit += 1
    return hit / len(left)


def window_agreement(tokens, base, lo, hi, sentences):
    """窗口转写与「全片句级转写同一区段」的吻合度，用来识别幽灵文本。

    局部窗口会稳定吐固定幽灵串（字数不少，实测 19 个 token），也会整段崩成十几个
    字。两者都表现为「和该区段的全片转写几乎不重叠」，所以拿 sentences.json 做
    交叉校验比按长度设阈值可靠。参考里带幽灵串的句子先剔除，避免自证。

    判定一律按**整窗**，不按选中句区间取字符：幽灵串的时间戳会铺满整窗，只取
    [t0,t1] 常常把 marker 切在区间外（实测 64-76 窗口因此被判 0.25 而不是 -1）。
    """
    text = norm("".join(str(x.get("w", "")) for x in tokens))
    if len(text) < 4:
        return -1.0
    for marker in HALLUCINATION_MARKERS:
        if norm(marker) in text:
            return -1.0
    reference = norm("".join(
        q.get("text", "") for q in (sentences or [])
        if q["end"] > lo and q["start"] < hi
        and not any(norm(m) in norm(q.get("text", "")) for m in HALLUCINATION_MARKERS)))
    if len(reference) < 8:
        return 1.0
    return text_overlap(text, reference)


def resolve_window(d, sr, t0, t1, sentences, backend, model_path, tmpdir, tag,
                   cache, identity, log=print):
    """为选中句拿到一份可信的局部词级转写。

    返回 (tokens, base, lo, hi, note, attempts)，note ∈
      "cache"    首选窗口命中缓存且吻合（最理想，零转写）
      "fresh"    首选窗口本次转写且吻合
      "moved"    首选窗口是幽灵文本，换落点后成功
      "degraded" 候选不可用/全不吻合，返回吻合度最高的一份（不静默返回垃圾，由调用方告警）

    缓存里可能存着早先版本写进去的幽灵条目，所以命中缓存也要重新过一遍吻合度判定，
    不合格就当场删掉重转 —— 否则幽灵会被永久缓存下来，每轮都白撞一次。
    """
    attempts, moved = 0, 0
    best = None
    candidates = window_candidates(t0, t1, sentences)
    if not candidates:
        # 走到这里说明该段长得连一个覆盖它的窗口都构造不出来。返回空词表让调用方
        # 走静音核/放宽兜底，而不是带着 None 往下解包崩掉。
        log(f"  !! 选中句 {t0:.1f}-{t1:.1f}（{t1 - t0:.1f}s）没有任何可用窗口，"
            f"该段切点不可信；选段过长时请拆成完整句")
        return [], window_base(t0, sr), round(float(t0), 3), round(float(t1), 3), \
            "degraded", 0
    for index, (lo, hi) in enumerate(candidates):
        key = window_key(identity, backend, model_path, lo, hi)
        entry = cache.get(key)
        if entry is not None:
            tokens = entry.get("words") or []
            base = entry.get("base", window_base(lo, sr))
        else:
            tokens = local_tokens(d, sr, lo, hi, backend, model_path, tmpdir,
                                  tag=f"{tag}_{index}")
            base = window_base(lo, sr)
            attempts += 1
        score = window_agreement(tokens, base, lo, hi, sentences)
        if score >= AGREEMENT_FLOOR:
            if entry is None:
                cache[key] = {"s0": lo, "e0": hi, "base": base, "words": tokens}
            note = "cache" if index == 0 and entry is not None else \
                   ("fresh" if index == 0 else "moved")
            if note == "moved":
                log(f"  选中句 {t0:.1f}-{t1:.1f} 首选窗口是幽灵文本"
                    f" → 换 {lo:.1f}-{hi:.1f} 后吻合 {score:.2f}")
            return tokens, base, lo, hi, note, attempts
        if entry is not None:
            cache.pop(key, None)          # 清掉被污染的缓存条目
        if best is None or score > best[0]:
            best = (score, tokens, base, lo, hi)
        moved += 1
    score, tokens, base, lo, hi = best
    log(f"  !! 选中句 {t0:.1f}-{t1:.1f} 的 {moved} 个候选窗口都不吻合"
        f"（最佳 {score:.2f} @ {lo:.1f}-{hi:.1f}），该段切点可能不准")
    return tokens, base, lo, hi, "degraded", attempts


def audio_identity(path):
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def window_key(identity, backend, model, t0, t1):
    raw = "|".join([identity["path"], str(identity["size"]), str(identity["mtime_ns"]),
                    str(backend), str(model or ""), f"{t0:.3f}", f"{t1:.3f}"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def load_window_cache(path, identity):
    """音频换了就整份作废；否则返回 窗口键 -> {"s0","e0","words"}。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if data.get("audio") != identity:
        return {}
    return data.get("entries") or {}


def save_window_cache(path, identity, entries):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"audio": identity, "entries": entries}, handle, ensure_ascii=False)


def lcs(W, T):
    """最长公共子序列：返回 (匹配数, 首个匹配位, 末个匹配位, 是否吃完全部 T)。
    贪心顺序匹配遇到「一边多字一边少字」就断（SRT 写「超薄型」、ASR 说「超薄的」，
    多出的那个字会让后续全部错位）；LCS 允许两侧各自跳字，才扛得住 ASR 差异。
    done=False 表示 T 的尾部音节在词表里找不到对应 —— 尾部不可信，要多让一点。"""
    n, m = len(W), len(T)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if W[i] == T[j] else max(nxt[j], row[j + 1])
    i = j = k = 0
    first = last = None
    while i < n and j < m:
        if W[i] == T[j]:
            if first is None:
                first = i
            last = i
            k += 1
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return k, first, last, j >= m


def locate(words, T, anchor, alo, ahi, limit):
    """T 为拼音音节序列；words 每项带 y（拼音）。"""
    if not T:
        return None
    best = None
    for i, w in enumerate(words):
        if w["s"] > ahi:
            break
        if w["s"] < alo or w.get("y", w["w"]) != T[0]:
            continue
        j = i
        while j < len(words) and words[j]["s"] <= limit:
            j += 1
        k, first, last, done = lcs([x.get("y", x["w"]) for x in words[i:j]], T)
        if last is None:
            continue
        cov = k / len(T)
        key = (-cov, abs(words[i + first]["s"] - anchor))
        if best is None or key < best[0]:
            best = (key, cov, words[i + first]["s"], words[i + last]["te"], done)
    return None if best is None else (best[1], best[2], best[3], best[4])


def bounds(words, s, e, done):
    """把切点收进「字与字之间的空隙」里：头不越过上一字的结束，尾不越过下一字的开始。

    固定余量（-0.08 / +0.18）在相邻音节间隔≈0 时会吃进隔壁半个字，
    成片里就多出一个可辨认的小词 —— 实测「定制的」后面带出「多少」、
    「充绒量也不低」后面带出「保」（都是 +0.18 咬到了下一字的起音）。
    字表里没有邻居（片头片尾）时才用原始余量。
    """
    prev = [x["te"] for x in words if x["te"] <= s + 1e-6]
    nxt = [x["s"] for x in words if x["s"] >= e - 1e-6]
    s2 = max(0.0, s - 0.08)
    if prev:
        s2 = max(0.0, min(s, max(s2, max(prev) + 0.02)))
    e2 = e + (0.18 if done else 0.5)
    if nxt:
        e2 = max(e, min(e2, min(nxt) - 0.02))
    return round(s2, 3), round(e2, 3)


def align(words, text, raw_s, raw_e):
    """返回 (切点起, 切点止, 覆盖率, 定位方式)。

    尾部搜索窗口只放到 raw_e + 0.45：放宽会撞上主播把同一句话讲的第二遍
    （实测「它充绒量也不低」被重复讲，窗口一宽就命中第二遍，成片里出现重复句）。
    尾部音节没匹配上（ASR 把「加绒」听成「加入」）时，尾余量给到 0.5s 并吸附到
    token 边界，否则会切在半个词里，成片听起来就是「面料全部」后面没了。
    收尾一律过 bounds()：拿到的切点必须落在两字之间的真空隙里。

    覆盖率门槛 0.60（原 0.70）：SRT 文案常比实际口播多 2~3 个字（称呼、语气词，
    如「姐妹们」），实测「姐妹们去搭白牛去搭白牛」只匹配 8/12=0.67 就整段放弃对齐、
    退回原始宽窗口 —— 结果把前后各约 0.9s 静音全带进成片（1.8s 死气）。

    locate 还要求 T 的首字能在词表里找到锚点，SRT 多出来的开头（「姐妹们」）
    会让锚点直接找不到 → 同样退回宽窗口。所以再套一层：开头最多丢 3 个字重试，
    丢掉的那部分同步从文案里删掉（成片文案必须等于成片音频）。
    """
    T = to_syl(text)
    if not T or not words:
        return None
    for k in range(0, 4):
        if len(T) - k < 3:
            break
        b = locate(words, T[k:], raw_s, raw_s - 2.2, raw_s + 2.2, raw_e + 0.45)
        if b and b[0] >= (0.6 if k == 0 else 0.8):
            cov, s, e, done = b
            s2, e2 = bounds(words, s, e, done)
            return s2, e2, round(cov, 2), ("char" if k == 0 else f"char-{k}"), text[k:]
    if len(T) >= 3:
        h = locate(words, T[:3], raw_s, raw_s - 2.2, raw_s + 2.2, raw_s + 2.5)
        t = locate(words, T[-3:], raw_e, raw_e - 2.5, raw_e + 2.2, raw_e + 2.0)
        if h and t and h[0] >= 0.55 and t[0] >= 0.55:
            s2, e2 = bounds(words, h[1], t[2], t[3])
            return s2, e2, round(min(h[0], t[0]), 2), "headtail", text
    return None


def snap(cores, t, limit=0.6):
    """最近的静音核；限内没有就返回 None（不假装成功）。"""
    best = None
    for c in cores:
        dd = abs(c - t)
        if dd <= limit and (best is None or dd < best[0]):
            best = (dd, c)
    return None if best is None else round(best[1], 3)


def bad_in(seq, s, e):
    m = BAD_RE.search("".join(x["w"] for x in seq if s <= x["s"] <= e))
    return m.group(0) if m else ""


def parse_file_map(values, option):
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.isdigit() or not os.path.isfile(path):
            raise SystemExit(f"Invalid --{option}: {value!r}; expected ID=existing-json")
        result[int(key)] = path
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--media", help="audio 尚未提取时用于按需生成缓存 WAV 的源视频")
    ap.add_argument("picks")
    ap.add_argument("out")
    ap.add_argument("--words")
    ap.add_argument("--sentences")
    ap.add_argument("--sentence-map", action="append", default=[])
    ap.add_argument("--model")
    ap.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    ap.add_argument("--maxblk", type=float, default=3.5)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--selected-words-dir")
    ap.add_argument("--cache", help="逐窗口转写缓存，默认 <out 同目录>/align_cache.json")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    with open(a.picks, encoding="utf-8") as handle:
        raw = json.load(handle)
    cfg = raw if isinstance(raw, dict) else {"picks": raw}
    picks = cfg.get("picks") or []
    offsets = {int(k): v for k, v in (cfg.get("offsets") or {"1": [0.0, ""]}).items()}

    sr, d = None, None

    def ensure_audio():
        nonlocal sr, d
        if sr is not None and d is not None:
            return sr, d
        if not os.path.isfile(a.audio):
            if not a.media or not os.path.isfile(a.media):
                raise RuntimeError("边界兜底需要音频，但 audio 不存在且未提供 --media")
            os.makedirs(os.path.dirname(os.path.abspath(a.audio)), exist_ok=True)
            temporary = a.audio + ".tmp.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a.media, "-vn",
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", temporary],
                           check=True)
            os.replace(temporary, a.audio)
        sr, d = load_audio(a.audio)
        return sr, d
    tmpdir = a.tmp or os.path.join(os.path.dirname(os.path.abspath(a.out)), "_tmp")
    os.makedirs(tmpdir, exist_ok=True)

    wmap = {}
    if a.words:
        wmap[1] = a.words
    for k, v in (cfg.get("words") or {}).items():
        wmap[int(k)] = v
    per_src = {}
    for src, path in wmap.items():
        base = offsets.get(src, [0.0, ""])[0]
        with open(path, encoding="utf-8") as handle:
            per_src[src] = expand(json.load(handle), base)
        if a.verbose:
            print(f"逐字时间戳 src{src}: {len(per_src[src])} 字")

    sentence_files = parse_file_map(a.sentence_map, "sentence-map")
    if a.sentences:
        sentence_files.setdefault(1, a.sentences)
    sent_by_src = {}
    for src, path in sentence_files.items():
        base = offsets.get(src, [0.0, ""])[0]
        with open(path, encoding="utf-8") as handle:
            sentence_rows = json.load(handle)
        sent_by_src[src] = [dict(q, start=float(q["start"]) + base,
                                      end=float(q["end"]) + base)
                            for q in sentence_rows]

    # 逐选中句补转写。窗口只由这一段（选中句 + 句子边界 + 固定网格）决定，不再按
    # picks 顺序做邻接合并 —— 合并会让窗口随「这轮选了哪些段」变化，从而每轮重
    # 转写、同一句话不同轮转出不同文本。重叠窗口靠 align_cache.json 天然复用。
    COVER_MARGIN = 3.0        # 判断「现有词表是否已覆盖这段」的余量，与窗口大小无关
    need = []
    for r in picks:
        src = int(r.get("src", 1))
        if not any(float(r["start"]) - COVER_MARGIN <= x["s"] <= float(r["end"]) + COVER_MARGIN
                   for x in per_src.get(src, [])):
            need.append((src, float(r["start"]), float(r["end"])))
    if need:
        sr, d = ensure_audio()
        identity = audio_identity(a.audio)
        cache_path = a.cache or os.path.join(
            os.path.dirname(os.path.abspath(a.out)), "align_cache.json")
        cache = load_window_cache(cache_path, identity)
        stats = {"cache": 0, "fresh": 0, "moved": 0, "degraded": 0}
        attempts = 0
        for src, t0, t1 in sorted(need):
            ws, base, lo, hi, note, used = resolve_window(
                d, sr, t0, t1, sent_by_src.get(src, []), a.backend, a.model, tmpdir,
                f"{t0:.1f}_{t1:.1f}".replace(".", "p"), cache, identity)
            stats[note] += 1
            attempts += used
            per_src[src] = sorted(per_src.get(src, []) + expand(ws, base),
                                  key=lambda x: x["s"])
        save_window_cache(cache_path, identity, cache)
        print(f"局部补转写: {len(need)} 个选中句 / {attempts} 次转写 "
              f"/ {stats['cache']} 命中缓存 / {stats['moved']} 次幽灵文本换窗口"
              + (f" / !! {stats['degraded']} 个无吻合候选" if stats["degraded"] else ""))
        for src in per_src:
            seen, dedup = set(), []
            for x in per_src[src]:
                k = (round(x["s"], 3), x["w"])
                if k not in seen:
                    seen.add(k)
                    dedup.append(x)
            per_src[src] = dedup
            if a.verbose:
                print(f"  src{src} 词表 {len(dedup)} 字")

    # 全片静音扫描是纯 Python 逐样本计算。绝大多数片段已有可靠词表，不应为不会
    # 使用的兜底先扫描整条长音频；仅在首次字级对齐失败时延迟计算一次。
    cores = None

    rows, skipped = [], []
    for r in picks:
        src = int(r.get("src", 1))
        raw_s, raw_e = float(r["start"]), float(r["end"])
        dur = raw_e - raw_s
        ws = per_src.get(src, [])

        got = align(ws, r.get("text", ""), raw_s, raw_e)
        if got:
            s, e, score, mode, txt = got
            # 下界放到 0.30：SRT/whisper 的段跨度常把前后句的静音一起圈进来，
            # 真实句子时长可以只有段跨度的一半。卡 0.55 会把正确对齐误判成放宽。
            if 0.30 * dur <= (e - s) <= 1.7 * dur and s <= raw_e and e >= raw_s:
                rows.append({"src": src, "s": round(s, 3), "e": round(e, 3),
                             "raw_s": raw_s, "raw_e": raw_e,
                             "mode": mode, "need_manual": False,
                             "manual_approved": bool(r.get("manual_approved")),
                             "score": score, "text": txt, "role": r.get("role"),
                             "module": r.get("module", "body")})
                continue

        if cores is None:
            sr, d = ensure_audio()
            cores = silence_cores(d, sr)
            print(f"真实静音核（按需）: {len(cores)} 个")
        sn = snap(cores, raw_s, 0.6)
        se = snap(cores, raw_e, 0.6)
        if (sn is not None and se is not None and (se - sn) >= 0.6
                and (raw_s - sn) <= 0.3 and (se - raw_e) <= 0.5):
            rows.append({"src": src, "s": sn, "e": se, "mode": "snap",
                         "raw_s": raw_s, "raw_e": raw_e,
                         "need_manual": False, "score": 0.0, "text": r.get("text", ""),
                         "manual_approved": bool(r.get("manual_approved")),
                         "role": r.get("role"), "module": r.get("module", "body")})
            continue

        rs, re_ = round(max(0.0, raw_s - PAD_HEAD), 3), round(raw_e + PAD_TAIL, 3)
        # 放宽窗口按字表收缩：只去掉首尾静音，不动任何有字的区间。
        # 否则 SRT 段跨度里的 1~2s 空白会原样进成片（听感上就是「卡住」）。
        inside = [x for x in ws if x["s"] >= raw_s and x["s"] <= raw_e]
        if inside:
            a0, a1 = min(x["s"] for x in inside), max(x["te"] for x in inside)
            rs = round(max(rs, a0 - 0.15), 3)
            re_ = round(min(re_, a1 + 0.25), 3)
        hit = ""
        if ws:
            h = bad_in(ws, rs - 0.2, raw_s) or bad_in(ws, raw_e, re_ + 0.2)
            if h:
                hit = f"放宽会带入相邻句违禁词「{h}」"
        if not hit:
            for q in sent_by_src.get(src, []):
                if (q["start"] < raw_s and q["end"] > rs - 0.2) or \
                        (raw_e < q["start"] < re_ + 0.2):
                    m = BAD_RE.search(q["text"])
                    if m:
                        hit = f"放宽会带入相邻句违禁词「{m.group(0)}」"
        if hit:
            skipped.append({"src": src, "start": raw_s, "end": raw_e,
                            "text": r.get("text", ""), "why": hit})
            continue
        rows.append({"src": src, "s": rs, "e": re_, "mode": "relaxed",
                     "raw_s": raw_s, "raw_e": raw_e,
                     "need_manual": True, "score": 0.0, "text": r.get("text", ""),
                     "manual_approved": bool(r.get("manual_approved")),
                     "role": r.get("role"), "module": r.get("module", "body")})

    # 全片去重：同一时间段只能入选一次（严禁重复文本）
    dedup, seen = [], []
    for l in rows:
        dup = next((q for q in seen if l["src"] == q["src"]
                    and min(q["raw_e"], l["raw_e"]) - max(q["raw_s"], l["raw_s"]) > 0.2), None)
        if dup:
            skipped.append({"src": l["src"], "start": l["raw_s"], "end": l["raw_e"],
                            "text": l["text"], "why": "与已入选片段时间重叠（重复内容）"})
            continue
        seen.append(l)
        dedup.append(l)
    rows = dedup

    blocks = []
    for l in rows:
        if blocks:
            b = blocks[-1]
            if (b["src"] == l["src"] and b.get("role") == l.get("role")
                    and b.get("module") == l.get("module")
                    and abs(l["s"] - b["e"]) <= 0.45
                    and (l["e"] - b["s"]) <= a.maxblk):
                b["e"] = l["e"]
                b["raw_e"] = max(b["raw_e"], l["raw_e"])
                b["items"].append(l)
                b["need_manual"] = b["need_manual"] or l["need_manual"]
                b["manual_approved"] = (b.get("manual_approved", False)
                                        or l.get("manual_approved", False))
                if b.get("mode") != l.get("mode"):
                    b["mode"] = "block"
                continue
        blocks.append({"src": l["src"], "s": l["s"], "e": l["e"],
                       "raw_s": l["raw_s"], "raw_e": l["raw_e"],
                       "need_manual": l["need_manual"],
                       "manual_approved": l.get("manual_approved", False),
                       "mode": l.get("mode"),
                       "role": l.get("role"),
                       "module": l.get("module", "body"), "items": [l]})

    keep = []
    for b in blocks:
        if keep and keep[-1]["src"] == b["src"]:
            x = keep[-1]
            ov = min(x["raw_e"], b["raw_e"]) - max(x["raw_s"], b["raw_s"])
            if ov > 0.2 and b["raw_s"] > x["raw_s"]:
                # 原始选段本身就重叠 = 同一内容讲两遍，淘汰后段（严禁重复）
                skipped.append({"src": b["src"], "start": b["raw_s"], "end": b["raw_e"],
                                "text": b["items"][0]["text"],
                                "why": "与上一段选段重叠（重复内容）"})
                continue
            gap = b["s"] - x["e"]
            # 只在真正相邻时共享切点；乱序选段（把后段话术挪到开头）gap 是负数且很大，不能碰
            if -0.45 <= gap < 0.45:
                mid = round((x["e"] + b["s"]) / 2, 3)
                x["e"] = mid
                b["s"] = mid
        keep.append(b)
    blocks = keep

    tl = []
    for b in blocks:
        o = offsets.get(b["src"], [0.0, ""])[0]
        tl.append({"src": b["src"], "gstart": round(b["s"], 3), "gend": round(b["e"], 3),
                   "start": round(b["s"] - o, 3), "end": round(b["e"] - o, 3),
                   "dur": round(b["e"] - b["s"], 3),
                   "need_manual": b["need_manual"],
                   "manual_approved": b.get("manual_approved", False),
                   "mode": b.get("mode"),
                   "role": b.get("role"), "module": b.get("module", "body"),
                   "text": " / ".join(x["text"] for x in b["items"])})

    banned = [x for x in tl if BAD_RE.search(x["text"])]
    if banned:
        tl = [x for x in tl if not BAD_RE.search(x["text"])]
        for x in banned:
            m = BAD_RE.search(x["text"])
            skipped.append({"src": x["src"], "start": x["gstart"], "end": x["gend"],
                            "text": x["text"], "why": f"终检命中违禁词「{m.group(0)}」"})

    with open(a.out, "w", encoding="utf-8") as handle:
        json.dump(tl, handle, ensure_ascii=False, indent=1)
    manual = os.path.join(os.path.dirname(os.path.abspath(a.out)), "manual_fix.json")
    with open(manual, "w", encoding="utf-8") as handle:
        json.dump({"need_manual": [x for x in tl if x["need_manual"]], "skipped": skipped},
                  handle, ensure_ascii=False, indent=1)

    # 将入选窗口用到的原始 token 落盘，供 audit_bounds.py 审计。
    # 这一步不生成全片逐字表，且写回源内时间。
    selected_dir = a.selected_words_dir or os.path.join(
        os.path.dirname(os.path.abspath(a.out)), "selected_words")
    os.makedirs(selected_dir, exist_ok=True)
    with open(a.picks, "rb") as handle:
        picks_digest = hashlib.sha256(handle.read()).hexdigest()
    with open(a.out, "rb") as handle:
        timeline_digest = hashlib.sha256(handle.read()).hexdigest()
    selected_manifest = {"_meta": {
        "picks_sha256": picks_digest,
        "timeline_sha256": timeline_digest,
        "segments": len(tl),
    }}
    for src in sorted({int(x["src"]) for x in tl}):
        base = float(offsets.get(src, [0.0, ""])[0])
        intervals = [(float(x["gstart"]), float(x["gend"])) for x in tl if int(x["src"]) == src]
        tokens, seen_tokens = [], set()
        for x in per_src.get(src, []):
            key = (round(float(x.get("ts", x["s"])), 6),
                   round(float(x.get("te", x["e"])), 6), x.get("token", x["w"]))
            if key in seen_tokens:
                continue
            if any(key[1] > start - 0.4 and key[0] < end + 0.4 for start, end in intervals):
                seen_tokens.add(key)
                tokens.append({"s": round(key[0] - base, 3),
                               "e": round(key[1] - base, 3), "w": key[2]})
        word_path = os.path.join(selected_dir, f"src{src}_words.json")
        with open(word_path, "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, ensure_ascii=False, indent=1)
        selected_manifest[str(src)] = os.path.abspath(word_path)
    manifest_path = os.path.join(selected_dir, "word_map.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(selected_manifest, handle, ensure_ascii=False, indent=2)

    total = sum(x["dur"] for x in tl)
    nm = [x for x in tl if x["need_manual"]]
    print(f"cuts: {len(tl)} segments / {total:.2f}s / {len(skipped)} skipped / "
          f"{len(nm)} manual -> {a.out}; word map -> {manifest_path}")
    if a.verbose:
        for i, x in enumerate(tl):
            print(f"{i:02d} src{x['src']} {x['gstart']:.2f}-{x['gend']:.2f} "
                  f"{x['dur']:.2f}s {x['text']}")
        for item in skipped:
            print(f"skip {item['start']:.2f}-{item['end']:.2f}: "
                  f"{item['text']} <- {item['why']}")


if __name__ == "__main__":
    main()
