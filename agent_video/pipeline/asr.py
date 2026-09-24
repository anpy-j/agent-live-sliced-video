# -*- coding: utf-8 -*-
"""S1 的 ASR 部分：复用引擎自带的转写与术语纠错原语。

复用点：
  - ``prep.extract_audio`` 按源指纹抽取 16k 单声道 WAV（带缓存）；
  - ``asr_backend.Transcriber`` 出词级时间戳；
  - ``prep.whisper_full`` 落 sentences/words，并在词与句两侧用同一套
    ``glossary`` 术语纠错，切点不会因改写而失配。

不新增 VAD/端点检测，也不做本地启发式兜底。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from agent_video.engine.scripts import prep
from agent_video.engine.scripts.asr_backend import Transcriber

from .errors import AsrError


def probe_duration(media: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", media],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AsrError(f"ffprobe 读取时长失败：{result.stderr.strip()[-400:]}")
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise AsrError(f"ffprobe 返回的时长无法解析：{result.stdout!r}") from None


def transcribe(media: str, workdir: str, *, backend: str | None = None,
               model: str | None = None) -> tuple[list[dict], list[dict]]:
    """抽取音轨并转写，返回 ``(sentences, words)``。"""
    if not os.path.isfile(media):
        raise AsrError(f"素材不存在：{media}")
    os.makedirs(workdir, exist_ok=True)
    try:
        wav = prep.extract_audio(media, workdir)
        transcriber = Transcriber(backend, model)
        sentences, words = prep.whisper_full(wav, transcriber, want_words=True)
    except AsrError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一归类为 S1 失败
        raise AsrError(f"ASR 失败：{exc}") from exc
    if not words:
        raise AsrError("ASR 没有输出任何词级时间戳")
    return sentences, words


def load_transcript(words_json: str | None,
                    sentences_json: str | None) -> tuple[list[dict], list[dict]]:
    """从落盘的 words/sentences 直接复用，跳过 ASR（端到端复跑用）。"""
    words: list[dict] = []
    sentences: list[dict] = []
    if words_json:
        with open(words_json, encoding="utf-8") as handle:
            words = json.load(handle)
    if sentences_json:
        with open(sentences_json, encoding="utf-8") as handle:
            sentences = json.load(handle)
    if not words:
        raise AsrError("没有可用的词级时间戳（--words-json 为空）")
    return sentences, words
