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

from .ai import (
    ANTIGRAVITY_FALLBACK_MODELS,
    CODEX_MODELS,
    OPENCODE_FALLBACK_MODELS,
    WORKBUDDY_MODELS,
)
from .db import Store, utc_now
from .labeling import (
    activate as activate_labels, build_patch, merge_patch, prepare as prepare_labels,
    profile_summary,
)
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
        self.label_overrides = self.store.get_setting("label_overrides", {}) or {}
        self.label_profile = activate_labels(self.label_overrides)
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
        job_type = str(payload.get("job_type") or "direct").lower()
        if job_type == "timeline" or "timeline_data" in payload or payload.get("draft_path"):
            return self._create_timeline_job(payload)
        return self._create_direct_job(payload)

    def _create_direct_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(payload.get("source_path", ""))).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"素材文件不存在: {source}")
        title = str(payload.get("title") or source.stem).strip()[:120]
        if not title:
            raise ValueError("请填写成片名称")
        target_min = payload.get("target_min")
        target_max = payload.get("target_max")
        target_seconds_str = str(payload.get("target_seconds") or "").strip()
        if target_min is not None and target_max is not None:
            try:
                min_s = float(target_min)
                max_s = float(target_max)
                if min_s <= 0 or max_s < min_s:
                    raise ValueError("最长时长不能小于最短时长，且必须大于 0")
                target_seconds = f"{min_s:g}-{max_s:g}"
            except (ValueError, TypeError) as exc:
                raise ValueError(f"目标时长格式错误: {exc}") from exc
        elif target_seconds_str:
            target_seconds = target_seconds_str
        else:
            target_seconds = "70-90"
        raw_export = payload.get("export_dir")
        if raw_export is None or not str(raw_export).strip():
            raw_export = self.store.get_setting("export_dir")
        export_dir = self._resolve_export_dir(raw_export)
        if export_dir:
            self.store.set_setting("export_dir", export_dir)
        product_name = str(payload.get("product_name") or "").strip()[:60] or None
        export_mode = str(payload.get("export_mode") or "merge").strip().lower()
        if export_mode not in {"merge", "segments"}:
            raise ValueError("输出形态必须是 合并版(merge) 或 分段版(segments)")
        source_workspace = self._source_workspace(source)
        placeholder = source_workspace / "edits" / "pending"
        job_id = self.store.create_job(title=title, source_path=str(source),
                                       workspace=str(placeholder), job_type="direct",
                                       target_seconds=target_seconds,
                                       export_dir=export_dir,
                                       product_name=product_name,
                                       export_mode=export_mode)
        edit_name = f"{job_id}-{self._path_slug(title, 48)}"
        workspace = source_workspace / "edits" / edit_name
        workspace.mkdir(parents=True, exist_ok=True)
        self.store.update_job(job_id, workspace=str(workspace))
        self.runner.enqueue(job_id)
        return self.store.get_job(job_id) or {"id": job_id}

    def _create_timeline_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .timeline import load_virtual_timeline
        target = (payload.get("draft_path") or payload.get("timeline_path") or
                  payload.get("source_path") or payload.get("timeline_data"))
        if not target:
            raise ValueError("请指定剪映草稿目录或虚拟时间线文件")

        try:
            vt = load_virtual_timeline(target)
        except Exception as exc:
            raise ValueError(f"加载虚拟时间线失败: {exc}") from exc

        if not vt.segments:
            raise ValueError("虚拟时间线不包含任何有效片段")

        title = str(payload.get("title") or vt.title).strip()[:120]
        if not title:
            title = vt.title or "虚拟时间线剪辑"

        target_min = payload.get("target_min")
        target_max = payload.get("target_max")
        target_seconds_str = str(payload.get("target_seconds") or "").strip()
        if target_min is not None and target_max is not None:
            try:
                min_s = float(target_min)
                max_s = float(target_max)
                if min_s <= 0 or max_s < min_s:
                    raise ValueError("最长时长不能小于最短时长，且必须大于 0")
                target_seconds = f"{min_s:g}-{max_s:g}"
            except (ValueError, TypeError) as exc:
                raise ValueError(f"目标时长格式错误: {exc}") from exc
        else:
            target_seconds = target_seconds_str or "70-90"

        raw_export = payload.get("export_dir")
        if raw_export is None or not str(raw_export).strip():
            raw_export = self.store.get_setting("export_dir")
        export_dir = self._resolve_export_dir(raw_export)
        if export_dir:
            self.store.set_setting("export_dir", export_dir)
        product_name = str(payload.get("product_name") or "").strip()[:60] or None
        export_mode = str(payload.get("export_mode") or "merge").strip().lower()
        if export_mode not in {"merge", "segments"}:
            raise ValueError("输出形态必须是 合并版(merge) 或 分段版(segments)")

        digest = hashlib.sha256(json.dumps(
            vt.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
        root = self.workspace_root / "timelines" / f"{self._path_slug(vt.timeline_id)}-{digest}"
        (root / "shared").mkdir(parents=True, exist_ok=True)
        (root / "edits").mkdir(parents=True, exist_ok=True)

        dur_mins = max(1, int(round(vt.total_duration / 60)))
        source_display = f"{vt.title}（约 {dur_mins} 分钟）"
        placeholder = root / "edits" / "pending"
        job_id = self.store.create_job(title=title, source_path=source_display,
                                       workspace=str(placeholder), job_type="timeline",
                                       timeline_meta=vt.to_dict(),
                                       target_seconds=target_seconds,
                                       export_dir=export_dir,
                                       product_name=product_name,
                                       export_mode=export_mode)
        edit_name = f"{job_id}-{self._path_slug(title, 48)}"
        workspace = root / "edits" / edit_name
        workspace.mkdir(parents=True, exist_ok=True)
        # 将解析好的 virtual_timeline.json 写入任务 workspace
        vt_file = workspace / "virtual_timeline.json"
        vt_file.write_text(json.dumps(vt.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.update_job(job_id, workspace=str(workspace))
        self.runner.enqueue(job_id)
        return self.store.get_job(job_id) or {"id": job_id}

    def inspect_timeline(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        from .timeline import load_virtual_timeline
        if isinstance(payload, str):
            target = payload
        else:
            target = (payload.get("path") or payload.get("draft_path") or
                      payload.get("source_path") or payload.get("timeline_data"))
        if not target:
            raise ValueError("请提供时间线或草稿路径")
        vt = load_virtual_timeline(target)
        dur_mins = max(1, int(round(vt.total_duration / 60)))
        return {
            "valid": True,
            "timeline_id": vt.timeline_id,
            "title": vt.title,
            "total_duration": round(vt.total_duration, 3),
            "timeline_duration": round(vt.total_duration, 3),
            "source_duration": round(vt.source_duration, 3),
            "duration_text": f"约 {dur_mins} 分钟",
            "segment_count": len(vt.segments),
            "sources": vt.source_paths,
            "source_count": len(vt.source_paths),
        }

    def list_timelines(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        from .timeline import discover_virtual_timelines
        if isinstance(payload, str):
            target = payload
        else:
            target = (payload.get("path") or payload.get("draft_path") or
                      payload.get("source_path"))
        if not target:
            raise ValueError("请提供剪映草稿目录或时间线文件")
        discovered = discover_virtual_timelines(target)
        for item in discovered["timelines"]:
            total = float(item.get("timeline_duration") or 0)
            minutes, seconds = divmod(total, 60)
            item["duration_text"] = f"{int(minutes)}分{seconds:04.1f}秒"
        return discovered

    @staticmethod
    def _resolve_export_dir(value: Any) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.exists() and not path.is_dir():
            raise ValueError(f"导出位置不是文件夹: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

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
        outputs = sorted(folder.glob("*.mp4"),
                         key=lambda path: (path.stat().st_mtime_ns, path.name)) \
            if folder.is_dir() else []
        output = outputs[-1] if outputs else folder / "final.mp4"
        export_value = str(job.get("export_dir") or "").strip()
        export_folder = Path(export_value).expanduser() if export_value else None
        exported = (sorted(export_folder.glob("*.mp4"),
                           key=lambda path: (path.stat().st_mtime_ns, path.name))
                    if export_folder and export_folder.is_dir() else [])
        return {
            "folder": str(folder),
            "exists": bool(outputs),
            "output": str(output),
            "export_folder": str(export_folder) if export_folder else "",
            "exported": [
                {"title": f"成片（导出）· {path.name}", "path": str(path), "kind": "video"}
                for path in reversed(exported)
            ],
            "deliverables": [
                {"title": f"成片 · {path.name}", "path": str(path), "kind": "video"}
                for path in reversed(outputs)
            ],
        }

    @staticmethod
    def _read_workspace_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def job_clauses(self, job_id: str) -> dict[str, Any]:
        """S2 放行与 S3 判定明细，供流程页人工核验（只读任务工作目录产物）。"""
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError("任务不存在")
        workspace = Path(str(job.get("workspace") or ""))
        timeline = self._read_workspace_json(workspace / "timeline.json")
        filtered = self._read_workspace_json(workspace / "clauses.filtered.json")
        timeline_clauses = timeline.get("clauses") if isinstance(timeline, dict) else None
        filtered_clauses = filtered.get("clauses") if isinstance(filtered, dict) else None
        base = timeline_clauses or filtered_clauses or []
        s2_map = {c.get("id"): c for c in (filtered_clauses or []) if isinstance(c, dict)}
        final_map = {c.get("id"): c for c in (timeline_clauses or []) if isinstance(c, dict)}
        clauses: list[dict[str, Any]] = []
        for source in base:
            if not isinstance(source, dict):
                continue
            cid = source.get("id")
            s2 = s2_map.get(cid, source)
            final = final_map.get(cid, source)
            clauses.append({
                "id": cid,
                "start": round(float(final.get("start") or 0.0), 3),
                "end": round(float(final.get("end") or 0.0), 3),
                "text": str(final.get("text") or ""),
                "s2_usable": bool(s2.get("usable", False)),
                "s2_reason": str(s2.get("reason") or "") if not s2.get("usable") else "",
                "usable": bool(final.get("usable", False)),
                "reason": str(final.get("reason") or ""),
                "order": final.get("order"),
            })
        clauses.sort(key=lambda c: (c["start"], c["id"] if isinstance(c["id"], int) else 0))
        s2_passed = sum(1 for c in clauses if c["s2_usable"])
        usable = sum(1 for c in clauses if c["usable"])
        return {
            "job_id": job_id,
            "ready": bool(clauses),
            "counts": {"total": len(clauses), "s2_passed": s2_passed,
                       "s2_rejected": len(clauses) - s2_passed, "usable": usable,
                       "rejected_by_s3": max(0, s2_passed - usable)},
            "clauses": clauses,
        }

    def open_deliverable_folder(self, job_id: str) -> dict[str, Any]:
        """在系统文件管理器中打开任务成片文件夹；路径只从任务记录推导。"""
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        info = self.deliverable_info(job)
        folder = Path(info["export_folder"] or info["folder"])
        if not folder.is_dir():
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
        if kind == "dir":
            script = 'POSIX path of (choose folder with prompt "选择成片导出位置")'
        elif kind in {"timeline", "draft", "json"}:
            script = 'POSIX path of (choose file with prompt "选择剪映草稿或虚拟时间线文件")'
        else:
            script = 'POSIX path of (choose file with prompt "选择直播视频素材")'
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode:
            message = result.stderr.strip()
            if "User canceled" in message or "-128" in message:
                return {"cancelled": True}
            raise ValueError(message or "无法打开文件选择器")
        if kind == "dir":
            return self._picked_dir_result(result.stdout.strip())
        return self._picked_file_result(result.stdout.strip(), kind=kind)

    def _pick_video_file_macos(self) -> dict[str, Any]:
        return self._pick_file_macos(kind="video")

    def _pick_file_windows(self, kind: str = "video") -> dict[str, Any]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise ValueError("未找到 PowerShell，无法打开 Windows 文件选择器")
        if kind == "dir":
            script = r"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Opacity = 0
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择成片导出位置'
$dialog.ShowNewFolderButton = $true
$owner.Show()
$owner.Activate()
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath))
    }
} finally {
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}
"""
        elif kind in {"timeline", "draft", "json"}:
            title = "选择剪映草稿或虚拟时间线"
            filter_spec = ("时间线/草稿 (*.json;draft_content.json)|*.json;draft_content.json|所有文件 (*.*)|*.*")
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
        else:
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
        if kind == "dir":
            return self._picked_dir_result(raw_path)
        return self._picked_file_result(raw_path, kind=kind)

    def _pick_video_file_windows(self) -> dict[str, Any]:
        return self._pick_file_windows(kind="video")

    @staticmethod
    def _picked_file_result(raw_path: str, kind: str = "video") -> dict[str, Any]:
        path = Path(raw_path).resolve()
        if kind in {"timeline", "draft", "json"}:
            if not path.is_file() and not path.is_dir():
                raise ValueError("请选择存在的草稿目录或时间线文件")
            return {"cancelled": False, "path": str(path), "name": path.stem or path.name}
        allowed = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".ts"}
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise ValueError("请选择 MP4、MOV、MKV、M4V、AVI、WebM 或 TS 视频")
        return {"cancelled": False, "path": str(path), "name": path.stem}

    @staticmethod
    def _picked_video_result(raw_path: str) -> dict[str, Any]:
        return Application._picked_file_result(raw_path, kind="video")

    @staticmethod
    def _picked_dir_result(raw_path: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser().resolve()
        if path.exists() and not path.is_dir():
            raise ValueError("请选择一个文件夹")
        path.mkdir(parents=True, exist_ok=True)
        return {"cancelled": False, "path": str(path), "name": path.name or str(path)}

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
            self.runner.retry(job_id)
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
        if name == "rerun_video_stage":
            job_id = str(args.get("job_id", ""))
            stage_id = str(args.get("stage_id", ""))
            self.runner.rerun_stage(job_id, stage_id)
            return {"job_id": job_id, "stage_id": stage_id, "queued": True}
        raise KeyError(f"未知工具: {name}")

    def settings(self) -> dict[str, Any]:
        keys = ["engine_python", "skill_path", "mcp_enabled", "mcp_token",
                "ai_engine", "ai_provider", "ai_model", "jev_base_url", "export_dir"]
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
        result["ai_models"] = {
            "auto": [{"id": "auto", "name": "自动选择"}],
            "opencode": [
                {"id": model_id, "name": name}
                for model_id, name in [("auto", "默认配置"), *OPENCODE_FALLBACK_MODELS]
            ],
            "codex": [
                {"id": model_id, "name": name} for model_id, name in CODEX_MODELS
            ],
            "workbuddy": [
                {"id": model_id, "name": name} for model_id, name in WORKBUDDY_MODELS
            ],
            "antigravity": [
                {"id": model_id, "name": name}
                for model_id, name in ANTIGRAVITY_FALLBACK_MODELS
            ],
        }
        return result

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"engine_python", "skill_path", "mcp_enabled", "ai_engine", "ai_provider",
                   "ai_model", "jev_api_key", "jev_base_url", "export_dir"}
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

    # ------------------------------------------------------------------ labeling
    def label_profile_info(self) -> dict[str, Any]:
        return {**profile_summary(self.label_overrides), "active": self.label_profile}

    def list_label_sessions(self) -> dict[str, Any]:
        sessions = self.store.list_label_sessions()
        return {"sessions": [
            {key: value for key, value in session.items() if key != "clauses"}
            | {"clause_count": len(session.get("clauses") or [])}
            for session in sessions
        ]}

    def get_label_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_label_session(session_id)
        if not session:
            raise KeyError("标注会话不存在")
        return session

    def create_label_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(payload.get("source_path", ""))).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"素材文件不存在: {source}")
        session_id = self.store.create_label_session(source_path=str(source))
        workdir = self.workspace_root / "labels" / session_id
        try:
            clauses = prepare_labels(str(source), str(workdir))
        except Exception:
            self.store.delete_label_session(session_id)
            raise
        self.store.set_label_clauses(session_id, clauses)
        return self.get_label_session(session_id)

    def save_label_decisions(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.store.get_label_session(session_id):
            raise KeyError("标注会话不存在")
        decisions = payload.get("decisions")
        if not isinstance(decisions, dict):
            raise ValueError("decisions 必须是对象")
        self.store.set_label_decisions(session_id, decisions)
        return self.get_label_session(session_id)

    def delete_label_session(self, session_id: str) -> dict[str, Any]:
        return {"deleted": self.store.delete_label_session(session_id)}

    def label_patch(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.store.get_label_session(session_id)
        if not session:
            raise KeyError("标注会话不存在")
        decisions = payload.get("decisions")
        if not isinstance(decisions, dict):
            decisions = session.get("decisions") or {}
        patch = build_patch(session.get("clauses") or [], decisions)
        result: dict[str, Any] = {"patch": patch,
                                  "profile": profile_summary(self.label_overrides)}
        if payload.get("apply"):
            merged = merge_patch(self.label_overrides, patch)
            self.store.set_setting("label_overrides", merged)
            self.label_overrides = merged
            self.label_profile = activate_labels(merged)
            result["applied"] = {"overrides": merged,
                                 "summary": self.label_profile["summary"]}
            result["profile"] = profile_summary(merged)
        return result


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
            if path.startswith("/api/jobs/") and path.endswith("/clauses"):
                job_id = path[len("/api/jobs/"):-len("/clauses")].strip("/")
                if not job_id:
                    return self.json_response({"error": "job_id 不能为空"}, 400)
                try:
                    return self.json_response(self.app.job_clauses(job_id))
                except KeyError as exc:
                    return self.json_response({"error": str(exc)}, 404)
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
            if path == "/api/label/profile":
                return self.json_response(self.app.label_profile_info())
            if path == "/api/label/sessions":
                return self.json_response(self.app.list_label_sessions())
            if path.startswith("/api/label/sessions/"):
                session_id = path.removeprefix("/api/label/sessions/").strip("/")
                try:
                    return self.json_response(self.app.get_label_session(session_id))
                except KeyError as exc:
                    return self.json_response({"error": str(exc)}, 404)
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
            if path == "/api/timeline/inspect":
                return self.json_response(self.app.inspect_timeline(payload))
            if path == "/api/timeline/list":
                return self.json_response(self.app.list_timelines(payload))
            if path == "/api/jobs":
                return self.json_response(self.app.create_job(payload), 201)
            if path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4:
                    job_id, action = parts[2], parts[3]
                    if action == "cancel":
                        return self.json_response({"cancelled": self.app.runner.cancel(job_id)})
                    if action == "retry":
                        self.app.runner.retry(job_id)
                        return self.json_response({"queued": True})
                    if action == "restart":
                        self.app.runner.restart(job_id)
                        return self.json_response({"restarted": True})
                    if action == "delete":
                        return self.json_response({"deleted": self.app.runner.delete(job_id)})
                    if action == "delivered":
                        return self.json_response({"delivered": self.app.runner.mark_delivered(job_id)})
                    if action == "open-folder":
                        return self.json_response(self.app.open_deliverable_folder(job_id))
                    if action == "rerun-stage":
                        stage_id = str(payload.get("stage_id") or "")
                        self.app.runner.rerun_stage(job_id, stage_id)
                        return self.json_response({"queued": True, "stage_id": stage_id})
            if path == "/api/mcp/token":
                return self.json_response({"token": self.app.rotate_token()})
            if path == "/api/label/sessions":
                return self.json_response(self.app.create_label_session(payload), 201)
            if path.startswith("/api/label/sessions/") and path.endswith("/patch"):
                session_id = path[len("/api/label/sessions/"):-len("/patch")].strip("/")
                return self.json_response(self.app.label_patch(session_id, payload))
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
            if path.startswith("/api/label/sessions/"):
                session_id = path.removeprefix("/api/label/sessions/").strip("/")
                return self.json_response(self.app.save_label_decisions(session_id, payload))
            return self.json_response({"error": "Not found"}, 404)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        try:
            path = self.path.partition("?")[0]
            if path.startswith("/api/label/sessions/"):
                session_id = path.removeprefix("/api/label/sessions/").strip("/")
                if not session_id:
                    raise ValueError("session_id 不能为空")
                return self.json_response(self.app.delete_label_session(session_id))
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
