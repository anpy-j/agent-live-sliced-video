from __future__ import annotations

import json
import math
import shutil
import threading
from pathlib import Path

from .ai import JsonAI
from . import pipeline
from .store import SmartStore, STAGES


class SmartService:
    """Synchronous HTTP execution keeps ownership observable; independent state lock."""
    def __init__(self, root, *, ai=None, understand=None, render=None):
        self.data_root = Path(root).resolve() / "data" / "smart_v3"
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.store = SmartStore(self.data_root / "smart_v3.db")
        self.ai = ai or JsonAI()
        self.understand = understand or pipeline.understand
        self.render = render or pipeline.render
        self.lock = threading.Lock()

    def workspace(self, job_id):
        # IDs come from this store, never an unchecked HTTP path.
        self.store.get(job_id)
        raw = self.jobs_root / job_id
        if raw.is_symlink() or getattr(raw, "is_junction", lambda: False)():
            raise ValueError("Invalid linked V3 workspace")
        path = raw.resolve()
        if path.parent != self.jobs_root.resolve():
            raise ValueError("Invalid V3 workspace")
        return path

    def create_job(self, payload):
        source = Path(str(payload.get("source_path") or "")).expanduser().resolve()
        product = str(payload.get("product_name") or "").strip()[:120]
        if not source.is_file() or not product:
            raise ValueError("V3 requires an existing local media file and product name")
        low, high = float(payload.get("target_min", 70)), float(payload.get("target_max", 120))
        if not all(math.isfinite(v) for v in (low, high)) or not 1 <= low <= high <= 600:
            raise ValueError("V3 target must satisfy 1 <= min <= max <= 600")
        transcript = payload.get("transcript_path")
        if transcript and not Path(str(transcript)).expanduser().is_file():
            raise ValueError("Transcript file not found")
        clean = {"source_path": str(source), "product_name": product, "target_min": low, "target_max": high,
                 "transcript_path": str(Path(str(transcript)).expanduser().resolve()) if transcript else None}
        with self.lock:
            job = self.store.create(clean)
            self.workspace(job["id"]).mkdir()
            return job

    def list_jobs(self):
        return self.store.list()

    def get_job(self, job_id):
        return self.store.get(job_id)

    def artifact(self, job_id, name):
        job = self.store.get(job_id)
        if name not in {a["name"] for a in job["artifacts"]}:
            raise KeyError("V3 artifact not found")
        path = self.workspace(job_id) / name
        if path.resolve().parent != self.workspace(job_id) or not path.is_file():
            raise KeyError("V3 artifact missing")
        return path

    def run(self, job_id, *, retry=False):
        if not self.lock.acquire(blocking=False):
            raise ValueError("V3 is executing another operation; retry later")
        try:
            job = self.store.get(job_id)
            if job["status"] == "completed" and not retry:
                raise ValueError("Completed job requires explicit retry")
            if job["status"] not in {"draft", "failed", "completed", "running"}:
                raise ValueError("Invalid V3 state")
            # A 'running' row with no lock is recoverable after process interruption.
            workspace = self.workspace(job_id)
            job.update(status="running", error=None, artifacts=[], attempt=job["attempt"] + 1,
                       stages={s: {"status": "pending"} for s in STAGES})
            self.store.save(job)
            current = "S1"

            def stage(name, fn):
                nonlocal current
                current = name
                job["stages"][name] = {"status": "running"}
                self.store.save(job)
                result = fn()
                artifact = f"{name}.json"
                (workspace / artifact).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                job["stages"][name] = {"status": "completed", "result": result}
                job["artifacts"].append({"name": artifact, "url": f"/api/smart-v3/jobs/{job_id}/artifacts/{artifact}"})
                self.store.save(job)
                return result

            try:
                inp = job["input"]
                units = stage("S1", lambda: self.understand(inp["source_path"], workspace, inp["transcript_path"]))
                coarse = stage("S2", lambda: pipeline.coarse_filter(units, inp["product_name"]))
                profiles = stage("S3", lambda: pipeline.profile(self.ai, coarse["kept"], inp["product_name"]))
                compilation = stage("S4", lambda: pipeline.arrange(profiles, inp["target_min"], inp["target_max"]))
                reviewed = stage("S5", lambda: pipeline.review(self.ai, profiles, compilation, inp["target_min"], inp["target_max"], inp["product_name"]))
                if not reviewed["passed"]:
                    raise ValueError("Whole-film review failed after 3 attempts; rendering withheld")
                job["stages"]["S5"]["status"] = "rendering"
                self.store.save(job)
                output = self.render(inp["source_path"], workspace, reviewed["timeline"])
                if output.resolve().parent != workspace or not output.is_file():
                    raise ValueError("Renderer did not create a V3 artifact")
                job["artifacts"].append({"name": output.name, "url": f"/api/smart-v3/jobs/{job_id}/artifacts/{output.name}"})
                job["stages"]["S5"]["status"] = "completed"
                job["status"] = "completed"
            except Exception as exc:
                job["status"], job["error"] = "failed", str(exc)
                job["stages"][current]["status"] = "failed"
            return self.store.save(job)
        finally:
            self.lock.release()

    def delete_job(self, job_id):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Cannot delete while V3 is executing")
        try:
            path = self.workspace(job_id)
            # Do not follow directory junctions/symlinks outside V3.
            if path.exists() and (path.is_symlink() or getattr(path, "is_junction", lambda: False)()):
                raise ValueError("Refusing linked V3 workspace")
            if path.exists():
                shutil.rmtree(path)
            self.store.delete(job_id)
            return {"deleted": True}
        finally:
            self.lock.release()
