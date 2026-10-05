from __future__ import annotations

from pathlib import Path
from typing import Any

from .analyzer import ANALYZER_VERSION, analyze_reference_locally
from .store import ViralStore


class ViralPipelineService:
    """Facade exposed to HTTP without coupling to the legacy JobRunner."""

    def __init__(self, project_root: Path):
        self.root = Path(project_root).resolve()
        self.data_root = self.root / "data" / "viral_v2"
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.store = ViralStore(self.data_root / "viral_v2.db")

    def create_reference(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()[:160]
        transcript = str(payload.get("transcript") or "").strip()
        if not title:
            raise ValueError("请填写爆款样本名称")
        if len(transcript) < 20:
            raise ValueError("爆款文本过短，至少需要20个字符")
        likes = payload.get("likes")
        if likes in (None, ""):
            likes_value = None
        else:
            likes_value = int(likes)
            if likes_value < 0:
                raise ValueError("点赞量不能小于0")
        duration = payload.get("duration_seconds")
        if duration in (None, ""):
            duration_value = None
        else:
            duration_value = float(duration)
            if duration_value <= 0:
                raise ValueError("视频时长必须大于0")
        reference = self.store.create_reference(
            title=title, transcript=transcript, likes=likes_value,
            duration_seconds=duration_value,
            published_at=str(payload.get("published_at") or "").strip() or None,
        )
        if payload.get("analyze", True):
            reference = self.analyze_reference(reference["id"])
        return reference

    def analyze_reference(self, reference_id: str) -> dict[str, Any]:
        reference = self.store.get_reference(reference_id)
        if not reference:
            raise KeyError("爆款样本不存在")
        try:
            dna = analyze_reference_locally(reference)
            self.store.save_analysis(
                reference_id, analyzer_version=ANALYZER_VERSION,
                model="local-baseline", dna=dna,
            )
        except Exception as exc:
            self.store.mark_reference_failed(reference_id, str(exc))
            raise
        return self.store.get_reference(reference_id) or reference

    def list_references(self) -> dict[str, Any]:
        rows = self.store.list_references()
        ready = sum(item.get("status") == "ready" for item in rows)
        return {"references": rows, "total": len(rows), "ready": ready,
                "analyzer_version": ANALYZER_VERSION}

    def get_reference(self, reference_id: str) -> dict[str, Any]:
        reference = self.store.get_reference(reference_id)
        if not reference:
            raise KeyError("爆款样本不存在")
        return reference

    def delete_reference(self, reference_id: str) -> dict[str, Any]:
        return {"deleted": self.store.delete_reference(reference_id)}

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()[:160]
        if not title:
            raise ValueError("请填写V2任务名称")
        source_path = str(payload.get("source_path") or "").strip() or None
        source_text = str(payload.get("source_text") or "").strip() or None
        if not source_path and not source_text:
            raise ValueError("请提供素材路径或文本")
        source_kind = "text" if source_text else str(payload.get("source_kind") or "media")
        target = str(payload.get("target_seconds") or "70-120").strip()
        mode = str(payload.get("reference_mode") or "hybrid").strip().lower()
        if mode not in {"replicate", "hybrid", "free"}:
            raise ValueError("参考模式必须是 replicate、hybrid 或 free")
        raw_reference_ids = payload.get("reference_ids") or []
        if not isinstance(raw_reference_ids, list):
            raise ValueError("reference_ids 必须是数组")
        reference_ids = list(dict.fromkeys(
            str(item).strip() for item in raw_reference_ids if str(item).strip()
        ))
        if mode != "free" and not reference_ids:
            raise ValueError("请至少选择一个爆款学习样本")
        missing = []
        not_ready = []
        for reference_id in reference_ids:
            reference = self.store.get_reference(reference_id)
            if not reference:
                missing.append(reference_id)
            elif reference.get("status") != "ready":
                not_ready.append(reference.get("title") or reference_id)
        if missing:
            raise ValueError(f"爆款样本不存在：{', '.join(missing)}")
        if not_ready:
            raise ValueError(f"以下样本尚未完成解析：{', '.join(not_ready)}")
        pending_workspace = self.jobs_root / "pending"
        job = self.store.create_job(
            title=title, source_kind=source_kind, source_path=source_path,
            source_text=source_text, target_seconds=target,
            reference_mode=mode, reference_ids=reference_ids,
            workspace=str(pending_workspace),
        )
        workspace = self.jobs_root / job["id"]
        workspace.mkdir(parents=True, exist_ok=True)
        with self.store.connect() as con:
            con.execute("UPDATE viral_jobs SET workspace=?,updated_at=? WHERE id=?",
                        (str(workspace), job["updated_at"], job["id"]))
        return self.store.get_job(job["id"]) or job

    def list_jobs(self) -> dict[str, Any]:
        rows = self.store.list_jobs()
        return {"jobs": rows, "total": len(rows)}

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError("V2任务不存在")
        return job
