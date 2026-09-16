from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .db import Store, utc_now


class JobRunner:
    """Run the local engine with one creative decision and one real-video review."""

    def __init__(self, store: Store, project_root: Path):
        self.store = store
        self.project_root = Path(project_root)
        self.pending: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: dict[str, subprocess.Popen[str]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        for job in reversed(self.store.list_jobs()):
            if job["status"] in {"queued", "running"}:
                self.store.update_job(job["id"], status="queued")
                self.pending.put(job["id"])
        self._thread = threading.Thread(target=self._loop, name="slice-agent-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for process in list(self._active.values()):
            process.terminate()

    def enqueue(self, job_id: str) -> None:
        self.store.update_job(job_id, status="queued", error=None, finished_at=None)
        self.store.add_event(job_id, None, "info", "queued", "任务已加入执行队列")
        self.pending.put(job_id)

    def cancel(self, job_id: str) -> bool:
        process = self._active.get(job_id)
        if process and process.poll() is None:
            process.terminate()
        job = self.store.get_job(job_id)
        if not job:
            return False
        self.store.update_job(job_id, status="cancelled", finished_at=utc_now())
        self.store.add_event(job_id, job.get("current_stage"), "warning", "cancelled", "任务已取消")
        return True

    def submit(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        workspace = Path(job["workspace"])
        stage = job["current_stage"]
        if stage == "edit_plan":
            missing = {"main_product", "picks"} - set(payload)
            if missing:
                raise ValueError(f"缺少字段: {', '.join(sorted(missing))}")
            target = workspace / "engine" / "picks.json"
            title = "AI 音画编排"
        elif stage == "rough_cut":
            if payload.get("verdict") != "approve":
                raise ValueError("第一版粗剪节点请提交 verdict=approve；需要改剪时请修改选段后重试")
            target = workspace / "rough_cut_approved.json"
            title = "粗剪审片结论"
        else:
            raise ValueError("当前节点不接受外部决策")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.update_stage(job_id, stage, status="succeeded", progress=1,
                                message="决策已提交", finished_at=utc_now(), result={"file": str(target)})
        self.store.add_artifact(job_id, stage, "decision", title, target, "application/json")
        self.store.add_event(job_id, stage, "success", "decision_submitted", "已接收决策，任务重新排队")
        self.enqueue(job_id)
        return {"job_id": job_id, "accepted": True, "stage": stage}

    def packet(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        engine_work = Path(job["workspace"]) / "engine"
        summary = self._read_json(engine_work / "pipeline_summary.json", {})
        packet: dict[str, Any] = {
            "job_id": job_id,
            "title": job["title"],
            "mode": job["mode"],
            "optional_editing_preferences": job["brief"],
            "current_stage": job["current_stage"],
            "engine_state": summary.get("state"),
            "instruction": summary.get("instruction"),
            "constraints": summary.get("constraints"),
            "artifacts": job.get("artifacts", []),
        }
        if job["current_stage"] == "edit_plan":
            packet["candidate_digest"] = self._read_json(engine_work / "candidate_digest.json", [])
            packet["instruction"] = "选择一个最强成片方案；默认只做 1 个钩子，避免重复方案消耗渲染时间"
        elif job["current_stage"] == "rough_cut":
            packet["instruction"] = "观看低清粗剪；可以发布则提交 verdict=approve，系统再执行高清导出与完整 QC"
            packet["rough_cut_videos"] = [
                item for item in job.get("artifacts", [])
                if item.get("stage_id") == "rough_cut" and item.get("kind") == "video"
            ]
        return packet

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job = self.store.get_job(job_id)
                if job and job["status"] != "cancelled":
                    self._run(job)
            except Exception as exc:
                job = self.store.get_job(job_id)
                stage = job["current_stage"] if job else "material_index"
                self.store.stage_fail(job_id, stage, str(exc))
            finally:
                self.pending.task_done()

    def _run(self, job: dict[str, Any]) -> None:
        source = Path(job["source_path"]).expanduser().resolve()
        workspace = Path(job["workspace"])
        engine_work = workspace / "engine"
        workspace.mkdir(parents=True, exist_ok=True)
        engine_work.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            self.store.stage_fail(job["id"], "material_index", f"素材不存在: {source}")
            return
        picks = engine_work / "picks.json"
        proxy_marker = workspace / "proxy_complete.json"
        approval = workspace / "rough_cut_approved.json"
        if not picks.is_file():
            self._index_material(job, source, workspace, engine_work)
        elif not proxy_marker.is_file():
            self._build_proxy(job, source, workspace, engine_work, proxy_marker)
        elif approval.is_file():
            self._deliver(job, source, workspace, engine_work)
        else:
            self.store.stage_wait(job["id"], "rough_cut", "低清粗剪已就绪，等待基于真实视频的审片")

    def _index_material(self, job: dict[str, Any], source: Path, workspace: Path,
                        engine_work: Path) -> None:
        job_id = job["id"]
        self.store.stage_start(job_id, "material_index", "正在读取素材、转写并生成候选索引")
        metadata = self._probe(source)
        report = workspace / "source.json"
        report.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.add_artifact(job_id, "material_index", "metadata", "素材信息", report, "application/json")
        code, summary = self._execute(job_id, source, workspace, engine_work, "material_index", [])
        if summary.get("state") == "awaiting_picks":
            self.store.stage_done(job_id, "material_index", "素材索引与候选摘要已生成",
                                  {"source_seconds": summary.get("source_seconds"), "media": metadata})
            self.store.stage_wait(job_id, "edit_plan", "等待 Agent 完成一次音画编排决策",
                                  {"write": summary.get("write")})
            return
        self._fail_engine(job_id, "material_index", code, summary)

    def _build_proxy(self, job: dict[str, Any], source: Path, workspace: Path,
                     engine_work: Path, proxy_marker: Path) -> None:
        job_id = job["id"]
        self._ensure_done(job_id, "edit_plan", "Agent 已完成音画编排")
        self.store.stage_start(job_id, "validation", "正在对齐时间线并执行结构、边界与画面规则校验")
        options = ["--auto-approve-visual"] if job["mode"] == "fast" else [
            "--auto-approve-visual", "--qc", "technical", "--width", "720",
            "--height", "1280", "--crf", "25", "--preset", "veryfast",
            "--audio-bitrate", "128k",
        ]
        code, summary = self._execute(job_id, source, workspace, engine_work, "validation", options)
        if summary.get("state") != "complete" or not summary.get("publish_ready"):
            self._fail_engine(job_id, "validation", code, summary)
            return
        self.store.stage_done(job_id, "validation", "时间线、边界与画面规则校验通过",
                              {"segments": summary.get("segments"), "render": summary.get("render")})
        if job["mode"] == "fast":
            self.store.stage_start(job_id, "rough_cut", "快速模式复用一次高清渲染，不生成独立粗剪")
            self.store.stage_done(job_id, "rough_cut", "快速模式已跳过独立粗剪审片")
            self.store.stage_start(job_id, "delivery", "正在登记高清成片与完整 QC")
            final_path = self._save_final(job, workspace, summary)
            self.store.stage_done(job_id, "delivery", f"高清成片已生成：{final_path.name}",
                                  {**summary, "final_output": str(final_path)})
            self.store.update_job(job_id, status="completed", progress=100, current_stage="delivery",
                                  finished_at=utc_now(), error=None, engine_state="complete")
            return
        self.store.stage_start(job_id, "rough_cut", "正在登记低清粗剪")
        proxy_files = self._save_proxy(job, workspace, summary)
        marker_data = {"created_at": utc_now(), "files": [str(path) for path in proxy_files]}
        proxy_marker.write_text(json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.add_artifact(job_id, "rough_cut", "report", "粗剪摘要", proxy_marker, "application/json")
        self.store.stage_wait(job_id, "rough_cut", "低清粗剪已生成，请观看实际视频后确认", marker_data)

    def _deliver(self, job: dict[str, Any], source: Path, workspace: Path,
                 engine_work: Path) -> None:
        job_id = job["id"]
        self._ensure_done(job_id, "rough_cut", "粗剪已确认")
        self.store.stage_start(job_id, "delivery", "正在高清导出并执行完整 QC")
        code, summary = self._execute(job_id, source, workspace, engine_work, "delivery", ["--auto-approve-visual"])
        if summary.get("state") == "complete" and summary.get("publish_ready"):
            final_path = self._save_final(job, workspace, summary)
            self.store.stage_done(job_id, "delivery", f"高清成片已生成：{final_path.name}",
                                  {**summary, "final_output": str(final_path)})
            self.store.update_job(job_id, status="completed", progress=100, current_stage="delivery",
                                  finished_at=utc_now(), error=None, engine_state="complete")
            return
        self._fail_engine(job_id, "delivery", code, summary)

    def _execute(self, job_id: str, source: Path, workspace: Path, engine_work: Path,
                 stage: str, options: list[str]) -> tuple[int, dict[str, Any]]:
        entry = Path(self.store.get_setting("engine_path", "")) / "scripts" / "run_slice.py"
        if not entry.is_file():
            raise RuntimeError(f"切片引擎不存在: {entry}")
        engine_python = Path(self.store.get_setting("engine_python", sys.executable)).expanduser()
        if not engine_python.is_file():
            raise RuntimeError(f"引擎 Python 不存在: {engine_python}")
        command = [str(engine_python), str(entry), str(source), "--workdir", str(engine_work), *options]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        self._active[job_id] = process
        lines: list[str] = []
        assert process.stdout
        for line in process.stdout:
            lines.append(line.rstrip())
            if len(lines) > 180:
                lines.pop(0)
            self.store.update_stage(job_id, stage, progress=0.45,
                                    message=line.strip()[-180:] or "处理中")
        code = process.wait()
        self._active.pop(job_id, None)
        log = workspace / f"{stage}.log"
        log.write_text("\n".join(lines), encoding="utf-8")
        self.store.add_artifact(job_id, stage, "log", f"{stage} 执行日志", log, "text/plain")
        summary_path = engine_work / "pipeline_summary.json"
        summary = self._read_json(summary_path, {})
        if summary_path.exists():
            self.store.add_artifact(job_id, stage, "report", "流程摘要", summary_path, "application/json")
        self._discover_core_artifacts(job_id, engine_work)
        self.store.update_job(job_id, engine_state=summary.get("state"))
        return code, summary

    def _save_proxy(self, job: dict[str, Any], workspace: Path,
                    summary: dict[str, Any]) -> list[Path]:
        candidates = self._preview_candidates(summary.get("deliverables") or {})
        if not candidates:
            raise RuntimeError("低清渲染完成，但没有找到可审片视频")
        target_dir = workspace / "rough-cut"
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for index, source in enumerate(candidates, 1):
            suffix = "" if index == 1 else f"-{index}"
            target = target_dir / f"{self._safe_title(job['title'])}-粗剪{suffix}.mp4"
            shutil.copy2(source, target)
            self.store.add_artifact(job["id"], "rough_cut", "video", target.stem, target, "video/mp4")
            saved.append(target)
        return saved

    def _save_final(self, job: dict[str, Any], workspace: Path,
                    summary: dict[str, Any]) -> Path:
        candidates = self._preview_candidates(summary.get("deliverables") or {})
        if not candidates:
            raise RuntimeError("高清渲染完成，但没有找到最终视频")
        target_dir = workspace / "deliverables"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{self._safe_title(job['title'])}.mp4"
        shutil.copy2(candidates[0], target)
        self.store.add_artifact(job["id"], "delivery", "video", f"最终成片 · {target.name}", target, "video/mp4")
        return target

    @staticmethod
    def _preview_candidates(deliverables: dict[str, Any]) -> list[Path]:
        previews_value = deliverables.get("previews")
        previews = Path(previews_value) if previews_value else None
        if previews and previews.is_dir():
            files = sorted(previews.glob("*.mp4"))
            if files:
                return files[:1]
        body_value = deliverables.get("body")
        body = Path(body_value) if body_value else None
        return [body] if body and body.is_file() else []

    @staticmethod
    def _safe_title(title: str) -> str:
        clean = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", title).strip(" .")
        if clean.lower().endswith(".mp4"):
            clean = clean[:-4].rstrip()
        return clean[:100] or "未命名成片"

    def _discover_core_artifacts(self, job_id: str, work: Path) -> None:
        known = [
            ("candidate_digest.json", "candidates", "候选语句", "application/json", "material_index"),
            ("overview.jpg", "image", "全场概览", "image/jpeg", "material_index"),
            ("selected.jpg", "image", "入选画面", "image/jpeg", "validation"),
            ("selected_detail.jpg", "image", "画面细节", "image/jpeg", "validation"),
            ("timeline.json", "timeline", "剪辑时间线", "application/json", "edit_plan"),
            ("visual_review.json", "decision", "自动画面规则", "application/json", "validation"),
            ("visual_report.json", "report", "画面规则报告", "application/json", "validation"),
        ]
        for relative, kind, title, mime, stage in known:
            path = work / relative
            if path.is_file():
                self.store.add_artifact(job_id, stage, kind, title, path, mime)

    def _ensure_done(self, job_id: str, stage_id: str, message: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            return
        stage = next((x for x in job["stages"] if x["stage_id"] == stage_id), None)
        if stage and stage["status"] != "succeeded":
            self.store.stage_done(job_id, stage_id, message)

    def _fail_engine(self, job_id: str, stage: str, code: int,
                     summary: dict[str, Any]) -> None:
        state = summary.get("state")
        error = summary.get("error") or f"切片引擎未完成（状态 {state or 'missing'}，退出码 {code}）"
        self.store.stage_fail(job_id, stage, error, summary)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _probe(source: Path) -> dict[str, Any]:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate",
             "-of", "json", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "ffprobe 无法读取素材")
        data = json.loads(result.stdout)
        data["source_path"] = str(source)
        data["source_name"] = source.name
        return data
