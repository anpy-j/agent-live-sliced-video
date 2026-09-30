# -*- coding: utf-8 -*-
"""任务执行器：唯一处理路径 = ``agent_video.pipeline.run_pipeline``。

``JobRunner`` 只做三件事：把任务放进串行队列、执行精简管线、把每个阶段的进度、
错误与产物写回 ``Store``。编排、规则筛、AI 判定/排序、渲染原语全在
``agent_video/pipeline/`` 内；这里不再有任何 AI 降级链、局部重编或兜底。
"""
from __future__ import annotations

import os
import multiprocessing
import json
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db import Store, utc_now
from .pipeline import (PipelineError, next_available_dir, next_available_output,
                       group_segments_for_export, render_segment, run_pipeline,
                       run_pipeline_stage)

HEARTBEAT_SECONDS = 5.0

# 精简管线通过环境变量选择 AI 引擎/提供方/模型；这些键与 DB 设置一一对应。
_AI_ENV_KEYS = {
    "ai_engine": "PIPELINE_AI_ENGINE",
    "ai_provider": "PIPELINE_AI_PROVIDER",
    "ai_model": "PIPELINE_AI_MODEL",
    "judge_concurrency": "PIPELINE_JUDGE_CONCURRENCY",
    "judge_retries": "PIPELINE_JUDGE_RETRIES",
    "jev_api_key": "TYPESAFE_API_KEY",
    "jev_base_url": "TYPESAFE_BASE_URL",
}

# 生产默认值：S3 批次 4 并发 + 单批失败重试 1 次（依据 LARI-18 对比实验：4 并发约 2.9×
# 提速，8 并发边际收益小且内存翻倍）。管线代码自身默认仍为串行(1)，未设 DB 设置时
# 由这里注入生产默认，仍可用 DB 设置或环境变量回退成 1。
_AI_ENV_DEFAULTS = {
    "PIPELINE_JUDGE_CONCURRENCY": "4",
    "PIPELINE_JUDGE_RETRIES": "1",
}


class JobCancelled(RuntimeError):
    """用户显式取消，不应被记成失败。"""


def _windows_kill_on_close_job() -> Any:
    """把当前任务进程及未来后代放入随句柄关闭而终止的 Windows Job。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(handle)
        return None
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(handle)
        return None
    return handle


def _run_pipeline_child(result_queue: Any, source: str, workspace: str,
                        target_seconds: tuple[float, float],
                        only_stage: str | None = None,
                        output_stem: str | None = None,
                        product_name: str | None = None) -> None:
    """在可强制终止的独立进程中执行重计算管线。"""
    job_handle = _windows_kill_on_close_job()
    if os.name != "nt":
        os.setsid()

    def on_stage(stage: str, status: str, message: str) -> None:
        result_queue.put(("stage", stage, status, message))

    try:
        vt = None
        vt_file = Path(workspace) / "virtual_timeline.json"
        if vt_file.is_file():
            from .timeline import load_virtual_timeline
            vt = load_virtual_timeline(vt_file)

        if only_stage:
            manifest = run_pipeline_stage(
                source, workspace, only_stage, target_seconds=target_seconds,
                product_name=product_name, output_stem=output_stem, on_stage=on_stage)
        else:
            manifest = run_pipeline(source, workspace, target_seconds=target_seconds,
                                    output_stem=output_stem, product_name=product_name,
                                    on_stage=on_stage, virtual_timeline=vt)
    except PipelineError as exc:
        result_queue.put(("pipeline_error", str(exc)))
    except BaseException as exc:  # noqa: BLE001 - 跨进程回传未预期错误
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        result_queue.put(("success", manifest))
    finally:
        # 变量必须活到管线结束；任务被强制终止时 OS 会关闭句柄并清理所有后代。
        _ = job_handle


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
        self._process: multiprocessing.Process | None = None
        self._running = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        for job in self.store.list_recoverable_jobs():
            if job.get("run_stage"):
                self.store.prepare_stage_rerun(job["id"], job["run_stage"])
            else:
                self.store.reset_job(job["id"])
            self._enqueue(job["id"], event=False)
        self._thread = threading.Thread(target=self._loop, name="slice-agent-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        self._terminate_active_process()

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
        if self._current == job_id:
            self._terminate_active_process(job_id)
        stage_id = job.get("current_stage")
        if stage_id:
            self.store.update_stage(job_id, stage_id, status="cancelled", progress=0,
                                    message="任务已取消", finished_at=utc_now(), error=None)
        self.store.update_job(job_id, status="cancelled", finished_at=utc_now())
        self.store.add_event(job_id, stage_id, "warning", "cancelled", "任务已取消")
        return True

    def restart(self, job_id: str, clean: bool = True) -> None:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job["status"] in {"queued", "running"}:
            self.cancel(job_id)
        self._cancelled.discard(job_id)
        workspace = Path(job.get("workspace") or "")
        if workspace and workspace.exists():
            if clean:
                shutil.rmtree(workspace, ignore_errors=True)
                workspace.mkdir(parents=True, exist_ok=True)
            else:
                preserved = {"clauses.json", "timeline.json", "audio16k.wav",
                             "audio16k.wav.manifest.json", "deliverables"}
                for item in workspace.iterdir():
                    if item.name not in preserved:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
        elif workspace:
            workspace.mkdir(parents=True, exist_ok=True)
        self.store.reset_job(job_id)
        self.enqueue(job_id)

    def retry(self, job_id: str) -> None:
        """重新开始：保留已有 ASR/子句切分等确定性产物，从后续失败步骤重试。"""
        self.restart(job_id, clean=False)

    def rerun_stage(self, job_id: str, stage_id: str) -> None:
        """重跑 S2/S3/S4/S6；保留上游输入并清空会失效的下游节点数据。

        重跑选中节点后会**自动继续执行其下游节点直到出片**（见 ``_succeed_stage``），
        所以外层只需提交一次。
        """
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError("任务不存在")
        if stage_id not in {"filter", "judge", "order", "render"}:
            raise ValueError("仅支持重新执行 S2、S3、S4 或渲染")
        if job["status"] in {"queued", "running"}:
            self.cancel(job_id)
            with self._condition:
                self._condition.wait_for(lambda: self._current != job_id, timeout=5.0)
        self._cancelled.discard(job_id)
        workspace = Path(job.get("workspace") or "")
        required = {
            "filter": ("clauses.json", "S1 语音转写与切分"),
            "judge": ("clauses.filtered.json", "S2 规则粗筛"),
            "order": ("clauses.judged.json", "S3 AI 可用性判定"),
            "render": ("order.json", "S4 AI 排序编排"),
        }
        filename, label = required[stage_id]
        if not (workspace / filename).is_file():
            raise ValueError(f"{label}数据不存在，不能单独重跑该节点")
        for name in {
            "filter": ("clauses.filtered.json", "clauses.judged.json", "order.json"),
            "judge": ("clauses.judged.json", "order.json"),
            "order": ("order.json",),
            "render": (),
        }[stage_id]:
            (workspace / name).unlink(missing_ok=True)
        if stage_id in {"judge", "order"}:
            timeline_path = workspace / "timeline.json"
            if timeline_path.is_file():
                try:
                    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
                    for clause in timeline.get("clauses") or []:
                        clause.pop("order", None)
                    timeline_path.write_text(
                        json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")
                except (OSError, ValueError, AttributeError):
                    pass
        self.store.prepare_stage_rerun(job_id, stage_id)
        self._enqueue(job_id, event=False)

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
            "process_id": self._process.pid if active and self._process else None,
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
                with self._condition:
                    self._condition.notify_all()

    @staticmethod
    def _parse_target_seconds(value: Any) -> tuple[float, float]:
        parts = [p.strip() for p in re.split(r"[-–—~,:]", str(value or "").strip())]
        if len(parts) == 2:
            try:
                low, high = float(parts[0]), float(parts[1])
                if low > 0 and high >= low:
                    return low, high
            except (ValueError, TypeError):
                pass
        return (70.0, 90.0)

    def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        source = Path(job["source_path"])
        workspace = Path(job["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        heartbeat = self._start_heartbeat(job_id)
        target_seconds = self._parse_target_seconds(job.get("target_seconds"))
        only_stage = job.get("run_stage")

        def on_stage(stage: str, status: str, message: str) -> None:
            if job_id in self._cancelled:
                raise JobCancelled()
            self._active_stage[job_id] = stage
            if status == "start":
                self.store.stage_start(job_id, stage, message)
            else:
                self.store.stage_done(job_id, stage, message)

        output_stem = str(job.get("title") or "").strip() or None
        product_name = str(job.get("product_name") or "").strip() or None
        try:
            with self._ai_environment():
                manifest = self._execute_pipeline(
                    job_id, str(source), str(workspace), target_seconds, on_stage,
                    only_stage=only_stage, output_stem=output_stem,
                    product_name=product_name)
        except JobCancelled:
            if self.store.get_job(job_id):
                self._finish(job_id, "cancelled", "任务已取消")
        except PipelineError as exc:
            stage = self._active_stage.get(job_id, "asr")
            self._fail(job_id, stage, str(exc))
        except Exception as exc:  # noqa: BLE001 - 未预期错误也按失败停止，不兜底
            stage = self._active_stage.get(job_id, "asr")
            self._fail(job_id, stage, f"未预期错误：{exc}")
        else:
            if only_stage:
                self._succeed_stage(job_id, workspace, only_stage, manifest, job)
            else:
                self._succeed(job_id, workspace, manifest, export_dir=job.get("export_dir"),
                              export_mode=str(job.get("export_mode") or "merge"),
                              output_stem=output_stem, media=str(source))
        finally:
            heartbeat.set()

    def _execute_pipeline(self, job_id: str, source: str, workspace: str,
                          target_seconds: tuple[float, float],
                          on_stage: Any, *, only_stage: str | None = None,
                          output_stem: str | None = None,
                          product_name: str | None = None) -> dict[str, Any]:
        """执行子进程并把阶段事件同步回主服务；取消时终止整棵进程树。"""
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_run_pipeline_child,
            args=(result_queue, source, workspace, target_seconds, only_stage,
                  output_stem, product_name),
            name=f"pipeline-{job_id}",
        )
        process.start()
        self._process = process
        terminal: tuple[Any, ...] | None = None
        try:
            while terminal is None:
                if job_id in self._cancelled or not self._running:
                    self._terminate_process_tree(process)
                    raise JobCancelled()
                try:
                    message = result_queue.get(timeout=0.2)
                except queue.Empty:
                    if not process.is_alive():
                        # 给 Queue 的后台发送线程一个短暂的收尾机会。
                        try:
                            message = result_queue.get(timeout=0.5)
                        except queue.Empty:
                            raise RuntimeError(
                                f"管线子进程异常退出（exitcode={process.exitcode}）")
                    else:
                        continue
                if message[0] == "stage":
                    _, stage, status, text = message
                    on_stage(stage, status, text)
                else:
                    terminal = message
            process.join(timeout=2)
            if terminal[0] == "success":
                return terminal[1]
            if terminal[0] == "pipeline_error":
                raise PipelineError(terminal[1])
            raise RuntimeError(terminal[1])
        finally:
            if process.is_alive():
                self._terminate_process_tree(process)
            process.join(timeout=2)
            result_queue.close()
            self._process = None

    def _terminate_active_process(self, job_id: str | None = None) -> None:
        process = self._process
        if process and process.is_alive() and (job_id is None or self._current == job_id):
            self._terminate_process_tree(process)

    @staticmethod
    def _terminate_process_tree(process: multiprocessing.Process) -> None:
        """终止任务进程及其 ffmpeg/AI CLI 等后代，不留下幽灵计算。"""
        if not process.pid or not process.is_alive():
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        process.join(timeout=3)
        if process.is_alive():
            process.kill()

    @contextmanager
    def _ai_environment(self) -> Iterator[None]:
        """把 DB 中的 AI 设置注入管线所需的环境变量，跑完恢复原值。

        未配置 DB 设置时，用 ``_AI_ENV_DEFAULTS`` 注入生产默认（S3 并发/重试）；DB
        设置或调用方已有的环境变量优先，仍可回退成串行。
        """
        managed = set(_AI_ENV_KEYS.values()) | set(_AI_ENV_DEFAULTS)
        previous = {key: os.environ.get(key) for key in managed}
        for setting_key, env_key in _AI_ENV_KEYS.items():
            value = self.store.get_setting(setting_key)
            if value:
                os.environ[env_key] = str(value)
        for env_key, default in _AI_ENV_DEFAULTS.items():
            if not os.environ.get(env_key):
                os.environ[env_key] = default
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

    @staticmethod
    def _export_video(source: Path, export_dir: str, stem: str | None = None) -> Path:
        folder = Path(export_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        target = Path(next_available_output(str(folder), stem or source.stem))
        shutil.copy2(source, target)
        return target

    def _export_outputs(self, workspace: Path, manifest: dict[str, Any], export_dir: str,
                        export_mode: str, output_stem: str | None,
                        media: str | None) -> list[Path]:
        folder = Path(export_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        if export_mode == "segments":
            segments = manifest.get("segments") or []
            if not segments:
                raise RuntimeError("没有可导出的分段")
            sub = Path(next_available_dir(str(folder), output_stem or "segments"))
            sub.mkdir(parents=True, exist_ok=True)
            source = media or str(manifest.get("source") or "")
            exported: list[Path] = []
            segment_groups = group_segments_for_export(segments)
            for index, segment_group in enumerate(segment_groups, start=1):
                target = Path(next_available_output(str(sub), f"{index:02d}"))
                render_segment(source, segment_group, str(target), str(workspace), index=index)
                exported.append(target)
            return exported
        rendered = workspace / str(manifest.get("output") or "")
        if not rendered.is_file():
            raise RuntimeError(f"成片不存在：{rendered}")
        return [self._export_video(rendered, export_dir, stem=output_stem)]

    def _succeed(self, job_id: str, workspace: Path, manifest: dict[str, Any],
                 export_dir: str | None = None, export_mode: str = "merge",
                 output_stem: str | None = None, media: str | None = None) -> None:
        artifacts = [
            ("asr", "json", "子句时间线", workspace / "timeline.json", "application/json"),
            ("filter", "json", "规则筛结果", workspace / "clauses.filtered.json", "application/json"),
            ("judge", "json", "AI 判定结果", workspace / "clauses.judged.json", "application/json"),
            ("render", "json", "渲染清单", workspace / "manifest.json", "application/json"),
        ]
        for stage_id, kind, title, path, mime in artifacts:
            if path.is_file():
                self.store.add_artifact(job_id, stage_id, kind, title, path, mime)
        videos = sorted((workspace / "deliverables").glob("*.mp4"),
                        key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for video in videos:
            self.store.add_artifact(job_id, "render", "video", f"成片 · {video.name}",
                                    video, "video/mp4")
        if export_dir:
            try:
                exported = self._export_outputs(workspace, manifest, export_dir,
                                                export_mode, output_stem, media)
            except Exception as exc:  # noqa: BLE001 - 导出失败不影响任务成功
                self.store.add_event(job_id, "render", "warning", "export_failed",
                                     f"导出到自定义位置失败：{exc}")
            else:
                for path in exported:
                    self.store.add_artifact(job_id, "render", "video",
                                            f"成片（导出）· {path.name}", path, "video/mp4")
                self.store.add_event(job_id, "render", "success", "exported",
                                     f"已导出 {len(exported)} 个文件到 {export_dir}")
        self.store.bump_edit_count(job_id)
        self.store.update_job(job_id, status="completed", progress=100, run_stage=None,
                              current_stage="render", finished_at=utc_now(), error=None)
        self.store.add_event(job_id, "render", "success", "job_completed",
                             f"成片已交付：{manifest.get('output')}", manifest)

    def mark_delivered(self, job_id: str) -> bool:
        if not self.store.get_job(job_id):
            raise KeyError("任务不存在")
        return self.store.mark_delivered(job_id)

    def _succeed_stage(self, job_id: str, workspace: Path, stage_id: str,
                       result: dict[str, Any], job: dict[str, Any]) -> None:
        # 渲染是最后一个节点：直接按出片收尾（登记产物、导出、交付事件）。
        if stage_id == "render":
            self._succeed(job_id, workspace, result,
                          export_dir=job.get("export_dir"),
                          export_mode=str(job.get("export_mode") or "merge"),
                          output_stem=str(job.get("title") or "").strip() or None,
                          media=str(job.get("source_path") or ""))
            return
        outputs = {
            "filter": ("规则筛结果", workspace / "clauses.filtered.json"),
            "judge": ("AI 判定结果", workspace / "clauses.judged.json"),
            "order": ("AI 编排结果", workspace / "order.json"),
        }
        title, path = outputs[stage_id]
        if path.is_file():
            self.store.add_artifact(job_id, stage_id, "json", title, path, "application/json")
        self.store.add_event(job_id, stage_id, "success", "single_stage_completed",
                             "单节点重新执行完成", result)
        # 单节点重跑不再停在 waiting_input：自动继续到下一个节点，直到渲染出片。
        next_stage = {"filter": "judge", "judge": "order", "order": "render"}[stage_id]
        self.store.prepare_stage_rerun(job_id, next_stage)
        self._enqueue(job_id, event=False)

    def _fail(self, job_id: str, stage: str, message: str) -> None:
        self.store.update_stage(job_id, stage, status="failed", progress=0, message=message,
                               error=message, finished_at=utc_now())
        self.store.update_job(job_id, status="failed", current_stage=stage, run_stage=None,
                              error=f"[{stage}] {message}", finished_at=utc_now())
        self.store.add_event(job_id, stage, "error", "job_failed", message)

    def _finish(self, job_id: str, status: str, message: str) -> None:
        self.store.update_job(job_id, status=status, finished_at=utc_now())
        self.store.add_event(job_id, None, "warning", status, message)
