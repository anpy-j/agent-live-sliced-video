# -*- coding: utf-8 -*-
"""Cross-platform Whisper adapter shared by prep, cuts and QC."""
import importlib.util
import os
import platform
from pathlib import Path


def installed(module):
    return importlib.util.find_spec(module) is not None


def resolve_snapshot(value):
    """Accept a model snapshot, a Hugging Face cache root, or a repo/model name."""
    if not value:
        return value
    path = Path(os.path.expanduser(value))
    if not path.exists():
        return value
    if (path / "config.json").is_file():
        return str(path.resolve())
    snapshots = path / "snapshots"
    choices = [child for child in snapshots.iterdir()
               if child.is_dir() and (child / "config.json").is_file()] if snapshots.is_dir() else []
    if not choices:
        raise RuntimeError(f"Model directory has no usable config.json or snapshot: {path}")
    return str(max(choices, key=lambda child: child.stat().st_mtime_ns).resolve())


def choose_backend(requested=None, model=None):
    requested = (requested or os.environ.get("DOUYIN_WHISPER_BACKEND") or "auto").lower()
    if requested not in {"auto", "mlx", "faster"}:
        raise RuntimeError("ASR backend must be auto, mlx, or faster")
    hint = (model or os.environ.get("DOUYIN_WHISPER_MODEL") or "").lower()
    mac_arm = platform.system() == "Darwin" and platform.machine() == "arm64"
    if requested == "auto":
        if "mlx" in hint and mac_arm and installed("mlx_whisper"):
            requested = "mlx"
        elif "faster-whisper" in hint or "ctranslate" in hint:
            requested = "faster"
        elif mac_arm and installed("mlx_whisper"):
            requested = "mlx"
        elif installed("faster_whisper"):
            requested = "faster"
        elif installed("mlx_whisper"):
            requested = "mlx"
        else:
            raise RuntimeError("No ASR backend installed; install the project ASR dependencies")
    module = "mlx_whisper" if requested == "mlx" else "faster_whisper"
    if not installed(module):
        raise RuntimeError(f"Selected ASR backend '{requested}' is not installed; install requirements once")
    return requested


def resolve_config(backend=None, model=None):
    selected = choose_backend(backend, model)
    env_model = os.environ.get("DOUYIN_WHISPER_MODEL")
    value = model or env_model
    if model is None and value:
        lower = value.lower()
        if (selected == "faster" and "mlx" in lower) or \
                (selected == "mlx" and ("faster-whisper" in lower or "ctranslate" in lower)):
            value = None
    if model is not None:
        lower = model.lower()
        if selected == "faster" and "mlx" in lower:
            raise RuntimeError("An MLX model cannot be used with the faster-whisper backend")
        if selected == "mlx" and ("faster-whisper" in lower or "ctranslate" in lower):
            raise RuntimeError("A CTranslate2 model cannot be used with the MLX backend")
    if not value:
        value = "mlx-community/whisper-large-v3-turbo" if selected == "mlx" else "small"
    return {"backend": selected, "model": resolve_snapshot(value)}


class Transcriber:
    def __init__(self, backend=None, model=None, device=None, compute_type=None):
        config = resolve_config(backend, model)
        self.backend = config["backend"]
        self.model_name = config["model"]
        self._model = None
        if self.backend == "faster":
            from faster_whisper import WhisperModel
            # 默认 CPU/int8（跨平台最稳）；有独显时用 DOUYIN_WHISPER_DEVICE=cuda 打开。
            # 原实现把 device="cpu" 写死，Windows 装了显卡也用不上。
            device = device or os.environ.get("DOUYIN_WHISPER_DEVICE") or "cpu"
            compute_type = (compute_type or os.environ.get("DOUYIN_WHISPER_COMPUTE")
                            or ("float16" if device.startswith("cuda") else "int8"))
            self.device = device
            self.compute_type = compute_type
            self._model = WhisperModel(self.model_name, device=device,
                                       compute_type=compute_type,
                                       cpu_threads=max(1, min(8, os.cpu_count() or 4)))

    @property
    def identity(self):
        return {"backend": self.backend, "model": self.model_name}

    def transcribe(self, audio, language="zh", word_timestamps=False,
                   initial_prompt=None, beam_size=5):
        """转写一段音频。

        `beam_size` 只有 faster-whisper 支持：MLX 走贪心解码，它的 DecodingOptions
        没有 beam_size 字段，硬塞会 TypeError。所以同一素材在 Apple Silicon 与
        Windows 上可能出不同文本；要求跨平台一致时显式 `--backend faster`。
        """
        if self.backend == "mlx":
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=self.model_name, language=language, verbose=False,
                word_timestamps=word_timestamps, condition_on_previous_text=False,
                initial_prompt=initial_prompt)
            rows = []
            for segment in result.get("segments", []):
                words = [{"s": float(word["start"]), "e": float(word["end"]),
                          "w": (word.get("word") or "").strip()}
                         for word in segment.get("words", []) if (word.get("word") or "").strip()]
                rows.append({"start": float(segment["start"]), "end": float(segment["end"]),
                             "text": (segment.get("text") or "").strip(), "words": words})
            return rows

        segments, _ = self._model.transcribe(
            audio, language=language, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400}, beam_size=beam_size,
            condition_on_previous_text=False, word_timestamps=word_timestamps,
            initial_prompt=initial_prompt)
        rows = []
        for segment in segments:
            words = [{"s": float(word.start), "e": float(word.end),
                      "w": (word.word or "").strip()}
                     for word in (segment.words or []) if (word.word or "").strip()]
            rows.append({"start": float(segment.start), "end": float(segment.end),
                         "text": (segment.text or "").strip(), "words": words})
        return rows
