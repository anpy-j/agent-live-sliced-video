from __future__ import annotations

import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .db import Store, utc_now
from .mcp import McpEndpoint, tool_specs
from .runner import JobRunner


class Application:
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
        defaults = {
            "engine_path": "/Volumes/MacData/Users/anpy/develop/personal/自媒体/切片/douyin-womenswear-slicing",
            "engine_python": str(self.root / ".venv" / "bin" / "python"),
            "skill_path": str(self.root / "integrations" / "skill" / "SKILL.md"),
            "mcp_enabled": True,
            "mcp_token": secrets.token_urlsafe(24),
            "max_parallel_jobs": 1,
        }
        for key, value in defaults.items():
            if self.store.get_setting(key) is None:
                self.store.set_setting(key, value)

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(payload.get("source_path", ""))).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"素材文件不存在: {source}")
        mode = payload.get("mode") or "standard"
        if mode not in {"fast", "standard", "refined"}:
            raise ValueError("mode 必须是 fast、standard 或 refined")
        title = str(payload.get("title") or source.stem).strip()[:120]
        if not title:
            raise ValueError("请填写成片名称")
        brief = str(payload.get("brief") or "").strip()
        placeholder = self.workspace_root / "pending"
        job_id = self.store.create_job(title=title, source_path=str(source), brief=brief,
                                       mode=mode, workspace=str(placeholder))
        workspace = self.workspace_root / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.store.update_job(job_id, workspace=str(workspace))
        self.runner.enqueue(job_id)
        return self.store.get_job(job_id) or {"id": job_id}

    def pick_video_file(self) -> dict[str, Any]:
        if sys.platform != "darwin":
            raise ValueError("当前系统暂不支持原生文件选择器")
        script = 'POSIX path of (choose file with prompt "选择直播视频素材")'
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode:
            message = result.stderr.strip()
            if "User canceled" in message or "-128" in message:
                return {"cancelled": True}
            raise ValueError(message or "无法打开文件选择器")
        path = Path(result.stdout.strip()).resolve()
        allowed = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".ts"}
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise ValueError("请选择 MP4、MOV、MKV、M4V、AVI、WebM 或 TS 视频")
        return {"cancelled": False, "path": str(path), "name": path.stem}

    def invoke_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "create_video_job":
            return self.create_job(args)
        if name == "list_video_jobs":
            return {"jobs": self.store.list_jobs()}
        if name == "get_video_job":
            job = self.store.get_job(str(args.get("job_id", "")))
            if not job:
                raise KeyError("任务不存在")
            return job
        if name == "get_stage_packet":
            return self.runner.packet(str(args.get("job_id", "")))
        if name == "submit_stage_payload":
            return self.runner.submit(str(args.get("job_id", "")), args.get("payload") or {})
        if name == "retry_video_job":
            job_id = str(args.get("job_id", ""))
            if not self.store.get_job(job_id):
                raise KeyError("任务不存在")
            self.runner.enqueue(job_id)
            return {"job_id": job_id, "queued": True}
        if name == "cancel_video_job":
            job_id = str(args.get("job_id", ""))
            return {"job_id": job_id, "cancelled": self.runner.cancel(job_id)}
        raise KeyError(f"未知工具: {name}")

    def settings(self) -> dict[str, Any]:
        keys = ["engine_path", "engine_python", "skill_path", "mcp_enabled", "mcp_token", "max_parallel_jobs"]
        return {key: self.store.get_setting(key) for key in keys}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"engine_path", "engine_python", "skill_path", "mcp_enabled", "max_parallel_jobs"}
        for key in allowed & payload.keys():
            self.store.set_setting(key, payload[key])
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
                return self.json_response(self.app.pick_video_file())
            if path == "/api/jobs":
                return self.json_response(self.app.create_job(payload), 201)
            if path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4:
                    job_id, action = parts[2], parts[3]
                    if action == "cancel":
                        return self.json_response({"cancelled": self.app.runner.cancel(job_id)})
                    if action == "retry":
                        self.app.runner.enqueue(job_id)
                        return self.json_response({"queued": True})
                    if action == "submit":
                        return self.json_response(self.app.runner.submit(job_id, payload))
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
