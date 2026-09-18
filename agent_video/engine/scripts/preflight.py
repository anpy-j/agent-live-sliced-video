# -*- coding: utf-8 -*-
"""Fast, cached, cross-platform environment validation for the slicing pipeline."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from asr_backend import installed, resolve_config


def cache_path():
    if platform.system() == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "livecut-womenswear-engine" / "preflight.json"


def signature(backend="auto", model=None):
    payload = {
        "python": sys.executable,
        "version": list(sys.version_info[:3]),
        "system": platform.system(),
        "machine": platform.machine(),
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "backend_request": backend,
        "model_request": model or os.environ.get("DOUYIN_WHISPER_MODEL"),
        "backend_env": os.environ.get("DOUYIN_WHISPER_BACKEND"),
        # 设备与量化档会改变转写结果，换档必须让缓存失效重检一次。
        "device_env": os.environ.get("DOUYIN_WHISPER_DEVICE"),
        "compute_env": os.environ.get("DOUYIN_WHISPER_COMPUTE"),
        "mlx_installed": installed("mlx_whisper"),
        "faster_installed": installed("faster_whisper"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), payload


def version_line(executable):
    result = subprocess.run([executable, "-version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=10)
    if result.returncode:
        raise RuntimeError(f"{Path(executable).name} cannot run")
    return (result.stdout or result.stderr).splitlines()[0][:240]


def check_environment(backend="auto", model=None, force=False, skip_asr=False,
                      stamp=None):
    stamp = Path(stamp) if stamp else cache_path()
    digest, facts = signature(backend, model)
    if not force and stamp.is_file():
        try:
            cached = json.loads(stamp.read_text(encoding="utf-8"))
            if cached.get("ok") and cached.get("signature") == digest:
                return dict(cached, cached=True, stamp=str(stamp))
        except (OSError, ValueError):
            pass

    issues = []
    if sys.version_info < (3, 10):
        issues.append("Python 3.10+ is required")
    for name in ("ffmpeg", "ffprobe"):
        if not facts[name]:
            issues.append(f"{name} is not available on PATH")

    config = {"backend": "skipped", "model": None}
    if not skip_asr:
        try:
            config = resolve_config(backend, model)
        except Exception as exc:  # concise diagnostic is written once, before production work
            issues.append(str(exc))

    versions = {}
    if not issues:
        try:
            versions = {name: version_line(facts[name]) for name in ("ffmpeg", "ffprobe")}
        except Exception as exc:
            issues.append(str(exc))

    report = {
        "ok": not issues,
        "cached": False,
        "signature": digest,
        "platform": {"system": facts["system"], "machine": facts["machine"]},
        "python": {"executable": sys.executable, "version": platform.python_version()},
        "asr": config,
        "executables": {"ffmpeg": facts["ffmpeg"], "ffprobe": facts["ffprobe"]},
        "versions": versions,
        "issues": issues,
        "stamp": str(stamp),
    }
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    parser.add_argument("--model")
    parser.add_argument("--stamp")
    parser.add_argument("--report")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-asr", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = check_environment(args.backend, args.model, args.force, args.skip_asr,
                               args.stamp)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    if report["ok"]:
        marker = "cached" if report.get("cached") else "checked"
        print(f"preflight: ok ({marker}); backend={report['asr']['backend']}")
        return
    print(f"preflight: failed; issues={len(report['issues'])}; report={args.report or report['stamp']}")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
