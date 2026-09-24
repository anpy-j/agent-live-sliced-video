# -*- coding: utf-8 -*-
"""任务执行器：唯一处理路径 = ``agent_video.pipeline.run_pipeline``。

``JobRunner`` 只做三件事：把任务放进串行队列、执行精简管线、把每个阶段的进度、
错误与产物写回 ``Store``。编排、规则筛、AI 判定/排序、渲染原语全在
``agent_video/pipeline/`` 内；这里不再有任何 AI 降级链、局部重编或兜底。
"""
from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db import Store, utc_now
from .pipeline import PipelineError, run_pipeline

HEARTBEAT_SECONDS = 5.0

# 精简管线通过环境变量选择 AI 引擎/提供方/模型；这些键与 DB 设置一一对应。
_AI_ENV_KEYS = {
    "ai_engine": "PIPELINE_AI_ENGINE",
    "ai_provider": "PIPELINE_AI_PROVIDER",
    "ai_model": "PIPELINE_AI_MODEL",
    "jev_api_key": "TYPESAFE_API_KEY",
    "jev_base_url": "TYPESAFE_BASE_URL",
}


class JobCancelled(RuntimeError):
    """用户显式取消，不应被记成失败。"""


class JobRunner:
    """串行执行精简管线的本地工作进程。"""

    def __init__(self, store: Store, project_root: Path):
        self.store = store
        self.project_root = Path(project_root)
        self._queue: list[str] = []
        self._queued: set[str] = set()
        self._cancelled: set[str] = set()
        self._active_stage: dict[str, str] = {}
        self._current: str | None = None
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        for job in self.store.list_recoverable_jobs():
            self.store.reset_job(job["id"])
            self._enqueue(job["id"], event=False)
        self._thread = threading.Thread(target=self._loop, name="slice-agent-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()

    def enqueue(self, job_id: str) -> None:
        self._enqueue(job_id, event=True)

    def _enqueue(self, job_id: str, *, event: bool) -> None:
        with self._condition:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
            self._queue.append(job_id)
            self._condition.notify_all()
        self.store.update_job(job_id, status="queued", error=None, finished_at=None)
        if event:
            self.store.add_event(job_id, None, "info", "queued", "任务已加入执行队列")

    def cancel(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if not job:
            return False
        if job["status"] not in {"queued", "running"}:
            return False
        self._cancelled.add(job_id)
        with self._condition:
            if job_id in self._queued:
                self._queued.discard(job_id)
                if job_id in self._queue:
                    self._queue.remove(job_id)
                self._condition.notify_all()
        stage_id = job.get("current_stage")
        if stage_id:
            self.store.update_stage(job_id, stage_id, status="cancelled", progress=0,
                                    message="任务已取消", finished_at=utc_now(), error=None)
        self.store.update_job(job_id, status="cancelled", finished_at=utc_now())
        self.store.add_event(job_id, stage_id, "warning", "cancelled", "任务已取消")
        return True

    def restart(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job["status"] in {"queued", "running"}:
            self.cancel(job_id)
        self._cancelled.discard(job_id)
        workspace = Path(job.get("workspace") or "")
        if workspace:
            shutil.rmtree(workspace, ignore_errors=True)
            workspace.mkdir(parents=True, exist_ok=True)
        self.store.reset_job(job_id)
        self.enqueue(job_id)

    def delete(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if not job:
            return False
        if job["status"] in {"queued", "running"}:
            self.cancel(job_id)
        with self._condition:
            self._queued.discard(job_id)
            if job_id in self._queue:
                self._queue.remove(job_id)
        self._cancelled.discard(job_id)
        workspace = Path(job.get("workspace") or "")
        if workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        return self.store.delete_job(job_id)

    def runtime(self, job_id: str) -> dict[str, Any]:
        active = self._current == job_id
        return {
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "process_active": active,
            "process_id": None,
            "queued": job_id in self._queued,
            "stage": self._active_stage.get(job_id),
        }

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while True:
            with self._condition:
                while self._running and not self._queue:
                    self._condition.wait(timeout=1.0)
                if not self._running:
                    return
                job_id = self._queue.pop(0)
                self._queued.discard(job_id)
            job = self.store.get_job(job_id)
            if not job:
                continue
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                continue
            self._current = job_id
            try:
                self._run(job)
            finally:
                self._current = None
                self._active_stage.pop(job_id, None)

    def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        source = Path(job["source_path"])
        workspace = Path(job["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        heartbeat = self._start_heartbeat(job_id)

        def on_stage(stage: str, status: str, message: str) -> None:
            if job_id in self._cancelled:
                raise JobCancelled()
            self._active_stage[job_id] = stage
            if status == "start":
                self.store.stage_start(job_id, stage, message)
            else:
                self.store.stage_done(job_id, stage, message)

        try:
            with self._ai_environment():
                manifest = run_pipeline(str(source), str(workspace), on_stage=on_stage)
        except JobCancelled:
            self._finish(job_id, "cancelled", "任务已取消")
        except PipelineError as exc:
            stage = self._active_stage.get(job_id, "asr")
            self._fail(job_id, stage, str(exc))
        except Exception as exc:  # noqa: BLE001 - 未预期错误也按失败停止，不兜底
            stage = self._active_stage.get(job_id, "asr")
            self._fail(job_id, stage, f"未预期错误：{exc}")
        else:
            self._succeed(job_id, workspace, manifest)
        finally:
            heartbeat.set()

    @contextmanager
    def _ai_environment(self) -> Iterator[None]:
        """把 DB 中的 AI 设置注入管线所需的环境变量，跑完恢复原值。"""
        previous = {key: os.environ.get(key) for key in _AI_ENV_KEYS.values()}
        for setting_key, env_key in _AI_ENV_KEYS.items():
            value = self.store.get_setting(setting_key)
            if value:
                os.environ[env_key] = str(value)
        try:
            yield
        finally:
            for env_key, old in previous.items():
                if old is None:
                    os.environ.pop(env_key, None)
                else:
                    os.environ[env_key] = old

    def _start_heartbeat(self, job_id: str) -> threading.Event:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                self.store.touch_job(job_id)

        threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True).start()
        return stop

    def _succeed(self, job_id: str, workspace: Path, manifest: dict[str, Any]) -> None:
        artifacts = [
            ("asr", "json", "子句时间线", workspace / "timeline.json", "application/json"),
            ("filter", "json", "规则筛结果", workspace / "clauses.filtered.json", "application/json"),
            ("judge", "json", "AI 判定结果", workspace / "clauses.judged.json", "application/json"),
            ("render", "json", "渲染清单", workspace / "manifest.json", "application/json"),
            ("render", "video", "成片", workspace / "deliverables" / "final.mp4", "video/mp4"),
        ]
        for stage_id, kind, title, path, mime in artifacts:
            if path.is_file():
                self.store.add_artifact(job_id, stage_id, kind, title, path, mime)
        self.store.update_job(job_id, status="completed", progress=100,
                              current_stage="render", finished_at=utc_now(), error=None)
        self.store.add_event(job_id, "render", "success", "job_completed",
                             f"成片已交付：{manifest.get('output')}", manifest)

    def _fail(self, job_id: str, stage: str, message: str) -> None:
        self.store.update_stage(job_id, stage, status="failed", progress=0, message=message,
                               error=message, finished_at=utc_now())
        self.store.update_job(job_id, status="failed", current_stage=stage,
                              error=f"[{stage}] {message}", finished_at=utc_now())
        self.store.add_event(job_id, stage, "error", "job_failed", message)

    def _finish(self, job_id: str, status: str, message: str) -> None:
        self.store.update_job(job_id, status=status, finished_at=utc_now())
        self.store.add_event(job_id, None, "warning", status, message)
