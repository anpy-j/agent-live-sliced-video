from __future__ import annotations

import base64
import json
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps single-process semantics only
    fcntl = None  # type: ignore[assignment]

from .db import Store, utc_now
from .mcp import McpEndpoint, tool_specs
from .runner import JobRunner


class Application:
    """本地服务：唯一处理路径是精简管线，这里只做任务登记、队列与产物展示。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data_dir = self.root / "data"
        self.workspace_root = self.root / "workspaces"
        self.web_root = self.root / "web"
        self.store = Store(self.data_dir / "agent.db")
        self.runner = JobRunner(self.store, self.root)
        self._defaults()
        self.mcp = McpEndpoint(self.invoke_tool)

    def _defaults(self) -> None:
        is_windows = sys.platform == "win32"
        venv_python = self.root / ".venv" / ("Scripts/python.exe" if is_windows else "bin/python")
        defaults = {
            "engine_python": str(venv_python),
            "skill_path": str(self.root / "integrations" / "skill" / "SKILL.md"),
            "mcp_enabled": True,
            "mcp_token": secrets.token_urlsafe(24),
            "ai_engine": "llm",
            "ai_provider": "auto",
            "ai_model": "auto",
            "jev_api_key": self._detect_jev_api_key(),
            "jev_base_url": "https://api.typesafe.ai/v1",
        }
        for key, value in defaults.items():
            if self.store.get_setting(key) is None:
                self.store.set_setting(key, value)

    @staticmethod
    def _detect_jev_api_key() -> str:
        env_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if env_key:
            return env_key
        demo_env = Path("/Volumes/MacData/Users/anpy/develop/personal/AI/project/typesafe-jev-demo/.env")
        if demo_env.is_file():
            try:
                for line in demo_env.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("TYPESAFE_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
        return ""

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(payload.get("source_path", ""))).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"素材文件不存在: {source}")
        title = str(payload.get("title") or source.stem).strip()[:120]
        if not title:
            raise ValueError("请填写成片名称")
        source_workspace = self._source_workspace(source)
        placeholder = source_workspace / "edits" / "pending"
        job_id = self.store.create_job(title=title, source_path=str(source),
                                       workspace=str(placeholder))
        edit_name = f"{job_id}-{self._path_slug(title, 48)}"
        workspace = source_workspace / "edits" / edit_name
        workspace.mkdir(parents=True, exist_ok=True)
        self.store.update_job(job_id, workspace=str(workspace))
        self.runner.enqueue(job_id)
        return self.store.get_job(job_id) or {"id": job_id}

    @staticmethod
    def _path_slug(value: str, limit: int = 64) -> str:
        slug = re.sub(r"[\\/:*?\"<>|\s]+", "-", value).strip("-. ")
        return (slug or "untitled")[:limit]

    def _source_workspace(self, source: Path) -> Path:
        identity = self._source_identity(source)
        digest = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
        root = self.workspace_root / "sources" / f"{self._path_slug(source.stem)}-{digest}"
        (root / "shared" / "indexes").mkdir(parents=True, exist_ok=True)
        (root / "edits").mkdir(parents=True, exist_ok=True)
        manifest = root / "source.json"
        temporary = root / ".source.json.tmp"
        temporary.write_text(json.dumps({
            "version": 1, "source": identity,
            "layout": {"shared": "shared", "edits": "edits"},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest)
        return root

    @staticmethod
    def _source_identity(source: Path) -> dict[str, Any]:
        """Use content bytes as well as metadata so replaced media cannot reuse a folder."""
        stat = source.stat()
        digest = hashlib.sha256()
        sample = 1024 * 1024
        with source.open("rb") as handle:
            digest.update(handle.read(sample))
            if stat.st_size > sample:
                handle.seek(max(0, stat.st_size - sample))
                digest.update(handle.read(sample))
        return {"path": str(source.resolve()), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "edge_sha256": digest.hexdigest()}

    def pick_file(self, kind: str = "video") -> dict[str, Any]:
        if sys.platform == "win32":
            return self._pick_file_windows(kind=kind)
        if sys.platform == "darwin":
            return self._pick_file_macos(kind=kind)
        raise ValueError("原生文件选择器目前仅支持 Windows 和 macOS")

    def pick_video_file(self) -> dict[str, Any]:
        return self.pick_file(kind="video")

    def deliverable_info(self, job: dict[str, Any]) -> dict[str, Any]:
        """成片输出目录与产物清单。"""
        workspace = Path(str(job.get("workspace") or ""))
        folder = workspace / "deliverables"
        output = folder / "final.mp4"
        return {
            "folder": str(folder),
            "exists": output.is_file(),
            "output": str(output),
            "deliverables": [{"title": "成片", "path": str(output), "kind": "video"}]
            if output.is_file() else [],
        }

    def open_deliverable_folder(self, job_id: str) -> dict[str, Any]:
        """在系统文件管理器中打开任务成片文件夹；路径只从任务记录推导。"""
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        info = self.deliverable_info(job)
        folder = Path(info["folder"])
        if not folder.is_dir():
            raise ValueError(f"成片文件夹尚未生成: {folder}")
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=False, timeout=30)
        else:
            subprocess.run(["xdg-open", str(folder)], check=False, timeout=30)
        return {"opened": True, "folder": str(folder)}

    def _pick_file_macos(self, kind: str = "video") -> dict[str, Any]:
        script = 'POSIX path of (choose file with prompt "选择直播视频素材")'
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode:
            message = result.stderr.strip()
            if "User canceled" in message or "-128" in message:
                return {"cancelled": True}
            raise ValueError(message or "无法打开文件选择器")
        return self._picked_file_result(result.stdout.strip(), kind=kind)

    def _pick_video_file_macos(self) -> dict[str, Any]:
        return self._pick_file_macos(kind="video")

    def _pick_file_windows(self, kind: str = "video") -> dict[str, Any]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise ValueError("未找到 PowerShell，无法打开 Windows 文件选择器")
        title = "选择直播视频素材"
        filter_spec = ("视频文件 (*.mp4;*.mov;*.mkv;*.m4v;*.avi;*.webm;*.ts)|"
                       "*.mp4;*.mov;*.mkv;*.m4v;*.avi;*.webm;*.ts|所有文件 (*.*)|*.*")
        script = rf"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Opacity = 0
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '{title}'
$dialog.Filter = '{filter_spec}'
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
$dialog.RestoreDirectory = $true
$owner.Show()
$owner.Activate()
try {{
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {{
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dialog.FileName))
    }}
}} finally {{
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}}
"""
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Windows 文件选择器等待超时，请重试并检查任务栏中的选择窗口") from None
        if result.returncode:
            raise ValueError(result.stderr.strip() or "无法打开 Windows 文件选择器")
        encoded_path = result.stdout.strip()
        if not encoded_path:
            return {"cancelled": True}
        try:
            raw_path = base64.b64decode(encoded_path, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Windows 文件选择器返回了无效路径") from exc
        return self._picked_file_result(raw_path, kind=kind)

    def _pick_video_file_windows(self) -> dict[str, Any]:
        return self._pick_file_windows(kind="video")

    @staticmethod
    def _picked_file_result(raw_path: str, kind: str = "video") -> dict[str, Any]:
        path = Path(raw_path).resolve()
        allowed = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".ts"}
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise ValueError("请选择 MP4、MOV、MKV、M4V、AVI、WebM 或 TS 视频")
        return {"cancelled": False, "path": str(path), "name": path.stem}

    @staticmethod
    def _picked_video_result(raw_path: str) -> dict[str, Any]:
        return Application._picked_file_result(raw_path, kind="video")

    def invoke_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "create_video_job":
            return self.create_job(args)
        if name == "list_video_jobs":
            return {"jobs": self.store.list_jobs()}
        if name == "get_video_job":
            job = self.store.get_job(str(args.get("job_id", "")))
            if not job:
                raise KeyError("任务不存在")
            job["deliverables"] = self.deliverable_info(job)
            return job
        if name == "retry_video_job":
            job_id = str(args.get("job_id", ""))
            if not self.store.get_job(job_id):
                raise KeyError("任务不存在")
            self.runner.restart(job_id)
            return {"job_id": job_id, "queued": True}
        if name == "restart_video_job":
            job_id = str(args.get("job_id", ""))
            self.runner.restart(job_id)
            return {"job_id": job_id, "restarted": True}
        if name == "delete_video_job":
            job_id = str(args.get("job_id", ""))
            return {"job_id": job_id, "deleted": self.runner.delete(job_id)}
        if name == "cancel_video_job":
            job_id = str(args.get("job_id", ""))
            return {"job_id": job_id, "cancelled": self.runner.cancel(job_id)}
        raise KeyError(f"未知工具: {name}")

    def settings(self) -> dict[str, Any]:
        keys = ["engine_python", "skill_path", "mcp_enabled", "mcp_token",
                "ai_engine", "ai_provider", "ai_model", "jev_base_url"]
        result = {key: self.store.get_setting(key) for key in keys}
        raw_jev_key = str(self.store.get_setting("jev_api_key") or "").strip()
        result["jev_api_key_configured"] = bool(raw_jev_key)
        if raw_jev_key:
            if len(raw_jev_key) <= 8:
                result["jev_api_key"] = "****"
            else:
                result["jev_api_key"] = f"{raw_jev_key[:3]}****{raw_jev_key[-4:]}"
        else:
            result["jev_api_key"] = ""
        result["engine_path"] = str(self.root / "agent_video" / "engine")
        result["engine_bundled"] = True
        return result

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"engine_python", "skill_path", "mcp_enabled", "ai_engine", "ai_provider",
                   "ai_model", "jev_api_key", "jev_base_url"}
        if "ai_engine" in payload and str(payload["ai_engine"]) not in {"llm", "jev"}:
            raise ValueError("ai_engine 必须是 llm 或 jev")
        if "ai_provider" in payload and str(payload["ai_provider"]) not in {
                "auto", "opencode", "codex", "workbuddy", "antigravity"}:
            raise ValueError("ai_provider 无效")
        for key in allowed & payload.keys():
            val = payload[key]
            if key == "jev_api_key":
                val_str = str(val or "").strip()
                if not val_str or "****" in val_str:
                    continue
                self.store.set_setting(key, val_str)
            elif key == "mcp_enabled":
                self.store.set_setting(key, bool(val))
            else:
                self.store.set_setting(key, str(val))
        return self.settings()

    def skill(self) -> dict[str, Any]:
        path = Path(self.store.get_setting("skill_path", ""))
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"path": str(path), "exists": path.is_file(), "content": content,
                "bytes": len(content.encode("utf-8"))}

    def save_skill(self, content: str) -> dict[str, Any]:
        path = Path(self.store.get_setting("skill_path", ""))
        if not content.lstrip().startswith("---") or "name:" not in content[:500] or "description:" not in content[:1000]:
            raise ValueError("Skill 必须包含带 name 和 description 的 YAML frontmatter")
        history = self.data_dir / "skill-history"
        history.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = history / f"SKILL-{utc_now().replace(':', '-')}.md"
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.skill()

    def mcp_info(self, host: str) -> dict[str, Any]:
        token = self.store.get_setting("mcp_token")
        url = f"http://{host}/mcp"
        return {
            "enabled": bool(self.store.get_setting("mcp_enabled", True)),
            "url": url,
            "token": token,
            "tools": tool_specs(),
            "configs": {
                "http": {"url": url, "headers": {"Authorization": f"Bearer {token}"}},
                "codex": {"mcp_servers": {"live-slicer": {"url": url, "bearer_token": token}}},
                "antigravity": {"mcpServers": {"live-slicer": {"url": url, "headers": {"Authorization": f"Bearer {token}"}}}},
                "workbuddy": {"name": "live-slicer", "transport": "streamableHttp", "url": url,
                              "headers": {"Authorization": f"Bearer {token}"}},
                "opencode": {"mcp": {"live-slicer": {"type": "remote", "url": url, "enabled": True,
                                                        "headers": {"Authorization": f"Bearer {token}"}}}},
            },
        }

    def rotate_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self.store.set_setting("mcp_token", token)
        return token


class Handler(BaseHTTPRequestHandler):
    server_version = "SliceAgent/0.1"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        try:
            path, _, query = self.path.partition("?")
            if path == "/api/health":
                return self.json_response({"ok": True, "version": "0.1.0"})
            if path == "/api/dashboard":
                return self.json_response(self.app.store.dashboard())
            if path == "/api/jobs":
                return self.json_response({"jobs": self.app.store.list_jobs()})
            if path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/").strip("/")
                job = self.app.store.get_job(job_id)
                if job:
                    job["runtime"] = self.app.runner.runtime(job_id)
                    job["deliverables"] = self.app.deliverable_info(job)
                return self.json_response(job or {"error": "任务不存在"}, 200 if job else 404)
            if path == "/api/settings":
                return self.json_response(self.app.settings())
            if path == "/api/skill":
                return self.json_response(self.app.skill())
            if path == "/api/mcp":
                return self.json_response(self.app.mcp_info(self.headers.get("Host", "127.0.0.1:8787")))
            if path.startswith("/api/artifacts/") and path.endswith("/content"):
                artifact_id = path.split("/")[3]
                return self.send_artifact(artifact_id)
            if path == "/mcp":
                return self.json_response({"name": "agent-live-sliced-video", "transport": "streamable-http", "hint": "Use POST JSON-RPC"}, 405)
            return self.send_static(path)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            path = self.path.partition("?")[0]
            payload = self.read_json()
            if path == "/api/files/pick":
                kind = str(payload.get("kind") or "video")
                return self.json_response(self.app.pick_file(kind=kind))
            if path == "/api/jobs":
                return self.json_response(self.app.create_job(payload), 201)
            if path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4:
                    job_id, action = parts[2], parts[3]
                    if action == "cancel":
                        return self.json_response({"cancelled": self.app.runner.cancel(job_id)})
                    if action == "retry":
                        self.app.runner.restart(job_id)
                        return self.json_response({"queued": True})
                    if action == "restart":
                        self.app.runner.restart(job_id)
                        return self.json_response({"restarted": True})
                    if action == "delete":
                        return self.json_response({"deleted": self.app.runner.delete(job_id)})
                    if action == "open-folder":
                        return self.json_response(self.app.open_deliverable_folder(job_id))
            if path == "/api/mcp/token":
                return self.json_response({"token": self.app.rotate_token()})
            if path == "/mcp":
                if not self.authorized_mcp():
                    return self.json_response({"error": "Unauthorized"}, 401)
                status, result = self.app.mcp.handle(payload)
                if result is None:
                    self.send_response(status)
                    self.end_headers()
                    return
                return self.json_response(result, status, {"MCP-Protocol-Version": "2025-06-18"})
            return self.json_response({"error": "Not found"}, 404)
        except (ValueError, KeyError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def do_PUT(self) -> None:
        try:
            path = self.path.partition("?")[0]
            payload = self.read_json()
            if path == "/api/settings":
                return self.json_response(self.app.update_settings(payload))
            if path == "/api/skill":
                return self.json_response(self.app.save_skill(str(payload.get("content", ""))))
            return self.json_response({"error": "Not found"}, 404)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        try:
            path = self.path.partition("?")[0]
            if path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/").strip("/")
                if not job_id:
                    raise ValueError("job_id 不能为空")
                deleted = self.app.runner.delete(job_id)
                return self.json_response({"deleted": deleted})
            self.send_error(404, "未找到端点")
        except (ValueError, KeyError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def authorized_mcp(self) -> bool:
        if not self.app.store.get_setting("mcp_enabled", True):
            return False
        expected = self.app.store.get_setting("mcp_token", "")
        return secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10 * 1024 * 1024:
            raise ValueError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def json_response(self, value: Any, status: int = 200,
                      headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else urllib.parse.unquote(path.lstrip("/"))
        target = (self.app.web_root / relative).resolve()
        if self.app.web_root not in target.parents and target != self.app.web_root:
            return self.send_error(403)
        if not target.is_file():
            target = self.app.web_root / "index.html"
        body = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_artifact(self, artifact_id: str) -> None:
        artifact = self.app.store.get_artifact(artifact_id)
        if not artifact:
            return self.send_error(404)
        path = Path(artifact["path"])
        if not path.is_file():
            return self.send_error(404)
        total = path.stat().st_size
        start, end, status = 0, total - 1, 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0]
            left, _, right = spec.partition("-")
            start = int(left or 0)
            end = min(int(right) if right else total - 1, total - 1)
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", artifact.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(path.name)}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: Application):
        super().__init__(address, Handler)
        self.app = app


def serve(root: Path, host: str = "127.0.0.1", port: int = 8787) -> None:
    lock_path = Path(root) / "data" / "agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+")
    if fcntl is not None:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            raise RuntimeError("已有一个 LiveCut 服务正在使用当前任务数据库") from None
    app = Application(root)
    app.runner.start()
    server = Server((host, port), app)
    print(f"Agent Live Sliced Video: http://{host}:{port}")
    print(f"MCP endpoint: http://{host}:{port}/mcp")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.runner.stop()
        server.server_close()
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
