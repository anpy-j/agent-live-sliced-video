from __future__ import annotations

import json
import mimetypes
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .db import STAGE_DEFINITIONS, Store, utc_now


class JobRunner:
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
        if stage == "creative_direction":
            target = workspace / "engine" / "picks.json"
            required = {"main_product", "picks"}
        elif stage == "visual_review":
            target = workspace / "engine" / "visual_review.json"
            required = {"decisions"}
            current = self._read_json(target, {})
            if current.get("_meta") and "_meta" not in payload:
                payload = {"_meta": current["_meta"], **payload}
        else:
            raise ValueError("当前节点不接受外部决策")
        missing = required - set(payload)
        if missing:
            raise ValueError(f"缺少字段: {', '.join(sorted(missing))}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.update_stage(job_id, stage, status="succeeded", progress=1,
                                message="外部 Agent 已提交决策", finished_at=utc_now(), result={"file": str(target)})
        self.store.add_artifact(job_id, stage, "decision", "Agent 决策", target, "application/json")
        self.store.add_event(job_id, stage, "success", "decision_submitted", "已接收决策，任务重新排队")
        self.enqueue(job_id)
        return {"job_id": job_id, "accepted": True, "stage": stage}

    def packet(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        workspace = Path(job["workspace"]) / "engine"
        summary = self._read_json(workspace / "pipeline_summary.json", {})
        packet: dict[str, Any] = {
            "job_id": job_id,
            "title": job["title"],
            "brief": job["brief"],
            "current_stage": job["current_stage"],
            "engine_state": summary.get("state"),
            "instruction": summary.get("instruction"),
            "constraints": summary.get("constraints"),
            "artifacts": job.get("artifacts", []),
        }
        digest = workspace / "candidate_digest.json"
        if job["current_stage"] == "creative_direction" and digest.exists():
            packet["candidate_digest"] = self._read_json(digest, [])
        if job["current_stage"] == "visual_review":
            timeline = workspace / "timeline.json"
            review = workspace / "visual_review.json"
            packet["timeline"] = self._read_json(timeline, [])
            packet["visual_review"] = self._read_json(review, {})
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
                stage = job["current_stage"] if job else "ingest"
                self.store.stage_fail(job_id, stage, str(exc))
            finally:
                self.pending.task_done()

    def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        source = Path(job["source_path"]).expanduser().resolve()
        workspace = Path(job["workspace"])
        engine_work = workspace / "engine"
        workspace.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            self.store.stage_fail(job_id, "ingest", f"素材不存在: {source}")
            return

        if job["stages"][0]["status"] != "succeeded":
            self.store.stage_start(job_id, "ingest", "正在读取媒体信息")
            metadata = self._probe(source)
            report = workspace / "source.json"
            report.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            self.store.add_artifact(job_id, "ingest", "metadata", "素材信息", report, "application/json")
            self.store.stage_done(job_id, "ingest", "素材读取完成", metadata)

        engine_work.mkdir(parents=True, exist_ok=True)
        if (engine_work / "visual_review.json").is_file() and job["current_stage"] == "visual_review":
            execution_stage = "final_render"
            execution_message = "正在应用画面决策、渲染成片并执行质检"
        elif (engine_work / "picks.json").is_file():
            execution_stage = "timeline"
            execution_message = "正在对齐选段、验证时间线并生成画面复核材料"
        else:
            execution_stage = "material_index"
            execution_message = "正在运行转写、候选提取与概览抽帧"
        self.store.stage_start(job_id, execution_stage, execution_message)
        engine = Path(self.store.get_setting("engine_path", ""))
        entry = engine / "scripts" / "run_slice.py"
        if not entry.is_file():
            self.store.stage_fail(job_id, "material_index", f"切片引擎不存在: {entry}")
            return
        engine_python = Path(self.store.get_setting("engine_python", sys.executable)).expanduser()
        if not engine_python.is_file():
            self.store.stage_fail(job_id, execution_stage,
                                  f"引擎 Python 不存在: {engine_python}；请运行 README 中的环境初始化命令")
            return
        command = [str(engine_python), str(entry), str(source), "--workdir", str(engine_work)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        self._active[job_id] = process
        lines: list[str] = []
        assert process.stdout
        for line in process.stdout:
            lines.append(line.rstrip())
            if len(lines) > 120:
                lines.pop(0)
            self.store.update_stage(job_id, execution_stage, progress=0.45,
                                    message=line.strip()[-180:] or "处理中")
        code = process.wait()
        self._active.pop(job_id, None)
        log = workspace / "agent-run.log"
        log.write_text("\n".join(lines), encoding="utf-8")
        self.store.add_artifact(job_id, "material_index", "log", "执行日志", log, "text/plain")
        summary_path = engine_work / "pipeline_summary.json"
        summary = self._read_json(summary_path, {})
        if summary_path.exists():
            self.store.add_artifact(job_id, "material_index", "report", "流程摘要", summary_path, "application/json")
        self._discover_artifacts(job_id, engine_work, summary)
        state = summary.get("state")
        self.store.update_job(job_id, engine_state=state)

        if state == "awaiting_picks":
            self.store.stage_done(job_id, "material_index", "素材索引与候选摘要已生成",
                                  {"source_seconds": summary.get("source_seconds"), "mode": summary.get("mode")})
            self.store.stage_wait(job_id, "creative_direction", "等待外部 Agent 生成创意方向与选段",
                                  {"write": summary.get("write")})
            return
        if state in {"awaiting_visual_review", "awaiting_broll_choices"}:
            for stage in ("material_index", "creative_direction", "timeline"):
                self._ensure_done(job_id, stage)
            self.store.stage_wait(job_id, "visual_review", "等待外部 Agent 完成画面复核",
                                  {"engine_state": state, "write": summary.get("write")})
            return
        if state == "complete" and summary.get("publish_ready"):
            for stage, name, _ in STAGE_DEFINITIONS:
                if stage == "quality_control":
                    break
                self._ensure_done(job_id, stage)
            self.store.stage_start(job_id, "quality_control", "正在登记最终质检结果")
            self.store.stage_done(job_id, "quality_control", "成片与质检全部完成", summary)
            self.store.update_job(job_id, status="completed", progress=100, current_stage="quality_control",
                                  finished_at=utc_now(), error=None)
            return
        if code != 0 or state == "failed":
            current = self._stage_for_failure(summary.get("step")) or execution_stage
            self.store.stage_fail(job_id, current, summary.get("error") or "切片引擎执行失败", summary)
            return
        self.store.stage_fail(job_id, execution_stage, f"未识别的引擎状态: {state or 'missing'}", summary)

    def _ensure_done(self, job_id: str, stage_id: str) -> None:
        job = self.store.get_job(job_id)
        stage = next(x for x in job["stages"] if x["stage_id"] == stage_id)
        if stage["status"] != "succeeded":
            self.store.stage_done(job_id, stage_id, "已由切片引擎完成")

    @staticmethod
    def _stage_for_failure(step: str | None) -> str:
        if not step:
            return "material_index"
        if "visual" in step or "broll" in step:
            return "visual_review"
        if "render" in step or "preview" in step:
            return "final_render"
        if "qc" in step:
            return "quality_control"
        if step in {"align", "validate", "audit"}:
            return "timeline"
        return "material_index"

    def _discover_artifacts(self, job_id: str, work: Path, summary: dict[str, Any]) -> None:
        known = [
            ("candidate_digest.json", "candidates", "候选语句", "application/json", "material_index"),
            ("overview.jpg", "image", "全场概览", "image/jpeg", "material_index"),
            ("selected-1.jpg", "image", "入选画面 1", "image/jpeg", "visual_review"),
            ("selected-2.jpg", "image", "入选画面 2", "image/jpeg", "visual_review"),
            ("timeline.json", "timeline", "剪辑时间线", "application/json", "timeline"),
            ("visual_review.json", "decision", "画面审核", "application/json", "visual_review"),
        ]
        for relative, kind, title, mime, stage in known:
            path = work / relative
            if path.is_file():
                self.store.add_artifact(job_id, stage, kind, title, path, mime)
        deliverables = summary.get("deliverables") or {}
        body = deliverables.get("body")
        if body and Path(body).is_file():
            self.store.add_artifact(job_id, "final_render", "video", "公共正文", Path(body), "video/mp4")
        for label, path in (deliverables.get("hooks") or {}).items():
            if Path(path).is_file():
                self.store.add_artifact(job_id, "final_render", "video", f"钩子 {label}", Path(path), "video/mp4")
        previews = Path(deliverables.get("previews", ""))
        if previews.is_dir():
            for path in sorted(previews.glob("*.mp4")):
                self.store.add_artifact(job_id, "rough_cut_review", "video", path.stem, path, "video/mp4")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _probe(source: Path) -> dict[str, Any]:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate", "-of", "json", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "ffprobe 无法读取素材")
        data = json.loads(result.stdout)
        data["source_path"] = str(source)
        data["source_name"] = source.name
        return data
