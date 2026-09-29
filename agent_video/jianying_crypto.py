"""Read encrypted Jianying draft JSON through the user's local videoeditor.dll.

The DLL call runs in a child process. Jianying's private C++ ABI can change
between releases; isolation prevents a mismatched DLL from crashing LiveCut.
The original draft is never modified.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


_DECRYPT_EXPORT = (
    "?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z"
)


class JianyingDecryptError(RuntimeError):
    """The local Jianying DLL could not decrypt a draft."""


def _version_key(path: Path) -> tuple[int, ...]:
    values = tuple(int(item) for item in re.findall(r"\d+", path.name))
    return values or (0,)


def find_jianying_install_dir(explicit: str | Path | None = None) -> Path:
    """Find a Jianying version directory that directly contains videoeditor.dll."""
    candidates: list[Path] = []
    for value in (explicit, os.environ.get("JIANYING_INSTALL_DIR"), os.environ.get("JY_INSTALL_DIR")):
        if value:
            candidates.append(Path(value).expanduser())

    local = os.environ.get("LOCALAPPDATA")
    if local:
        apps = Path(local) / "JianyingPro" / "Apps"
        if apps.is_dir():
            try:
                candidates.extend(sorted(
                    (item for item in apps.iterdir() if item.is_dir()),
                    key=_version_key,
                    reverse=True,
                ))
            except OSError:
                pass

    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name)
        if root:
            candidates.extend((Path(root) / "JianyingPro", Path(root) / "CapCut"))

    checked: list[str] = []
    for candidate in candidates:
        directory = candidate.resolve()
        dll = directory if directory.name.lower() == "videoeditor.dll" else directory / "videoeditor.dll"
        if dll.is_file():
            return dll.parent
        checked.append(str(directory))

    hint = "；".join(checked[:4]) or "未发现候选目录"
    raise JianyingDecryptError(
        "未找到剪映 videoeditor.dll。请确认已安装剪映专业版，或设置环境变量 "
        f"JIANYING_INSTALL_DIR 指向包含该 DLL 的版本目录。已检查：{hint}"
    )


def decrypt_jianying_file(
    source: str | Path,
    *,
    install_dir: str | Path | None = None,
    timeout: float = 90,
) -> dict[str, Any]:
    """Decrypt *source* without changing it and return the parsed JSON object."""
    if sys.platform != "win32":
        raise JianyingDecryptError("剪映 DLL 解密仅支持 Windows")
    source_path = Path(source).expanduser().resolve()
    directory = find_jianying_install_dir(install_dir)
    with tempfile.TemporaryDirectory(prefix="livecut-jianying-") as temporary:
        output = Path(temporary) / "draft.dec.json"
        command = [
            sys.executable, "-m", "agent_video.jianying_crypto", "--worker-decrypt",
            str(source_path), str(output), "--install-dir", str(directory),
        ]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=flags, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise JianyingDecryptError(f"剪映 DLL 解密超时（{timeout:g} 秒）") from exc
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout or f"退出码 {result.returncode}").strip()
            raise JianyingDecryptError(f"剪映 DLL 解密失败：{detail[-1000:]}")
        try:
            parsed = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JianyingDecryptError(f"DLL 已返回数据，但不是有效 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise JianyingDecryptError("DLL 解密结果不是 JSON 对象")
        return parsed


class _MsvcStringData(ctypes.Union):
    _fields_ = [("small", ctypes.c_char * 16), ("ptr", ctypes.c_void_p)]


class _MsvcString(ctypes.Structure):
    _fields_ = [
        ("data", _MsvcStringData),
        ("size", ctypes.c_uint64),
        ("capacity", ctypes.c_uint64),
    ]


class _StringArg:
    def __init__(self, raw: bytes):
        self.value = _MsvcString()
        self.value.size = len(raw)
        if len(raw) < 16:
            self.value.data.small = raw
            self.value.capacity = 15
            self.buffer = None
        else:
            self.buffer = ctypes.create_string_buffer(raw)
            self.value.data.ptr = ctypes.cast(self.buffer, ctypes.c_void_p).value
            self.value.capacity = len(raw)


def _worker_decrypt(source: Path, output: Path, install_dir: Path) -> None:
    if sys.platform != "win32":
        raise JianyingDecryptError("worker requires Windows")
    dll_path = install_dir / "videoeditor.dll"
    if not dll_path.is_file():
        raise JianyingDecryptError(f"videoeditor.dll 不存在：{dll_path}")

    os.environ["PATH"] = str(install_dir) + os.pathsep + os.environ.get("PATH", "")
    dll_cookie = os.add_dll_directory(str(install_dir))
    previous_cwd = Path.cwd()
    try:
        os.chdir(install_dir)
        library = ctypes.WinDLL(str(dll_path))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p
        address = kernel32.GetProcAddress(library._handle, _DECRYPT_EXPORT.encode("ascii"))
        if not address:
            raise JianyingDecryptError("当前剪映版本的 videoeditor.dll 缺少 decrypt 导出")
        prototype = ctypes.WINFUNCTYPE(
            ctypes.POINTER(_MsvcString), ctypes.c_void_p, ctypes.POINTER(_MsvcString),
            ctypes.POINTER(_MsvcString), ctypes.POINTER(_MsvcString), ctypes.POINTER(ctypes.c_bool),
        )
        decrypt = prototype(address)
        encrypted = _StringArg(source.read_bytes())
        params = _StringArg(b"{}")
        decrypted = _MsvcString()
        ok = ctypes.c_bool(False)
        decrypt(None, ctypes.byref(decrypted), ctypes.byref(encrypted.value),
                ctypes.byref(params.value), ctypes.byref(ok))
        if not ok.value or decrypted.size <= 0 or decrypted.size > 512 * 1024 * 1024:
            raise JianyingDecryptError(f"DLL 返回解密失败（ok={ok.value}, size={decrypted.size}）")
        pointer = ctypes.addressof(decrypted.data) if decrypted.capacity < 16 else decrypted.data.ptr
        plain = ctypes.string_at(pointer, decrypted.size)
        json.loads(plain.decode("utf-8-sig"))
        output.write_bytes(plain)
    finally:
        os.chdir(previous_cwd)
        dll_cookie.close()


def _main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-decrypt", action="store_true")
    parser.add_argument("source", nargs="?")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--install-dir")
    args = parser.parse_args()
    if not args.worker_decrypt or not args.source or not args.output or not args.install_dir:
        return 64
    try:
        _worker_decrypt(Path(args.source), Path(args.output), Path(args.install_dir))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
