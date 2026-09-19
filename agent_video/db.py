from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STAGE_DEFINITIONS = [
    ("material_index", "素材索引", 20),
    ("edit_plan", "AI 文本编排", 40),
    ("validation", "文本校验与原声锁定", 65),
    ("visual_mix", "多模态画面混剪", 80),
    ("delivery", "一次高清渲染", 100),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            try:
                yield con
                con.commit()
            finally:
                con.close()

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  source_path TEXT NOT NULL,
                  brief TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  current_stage TEXT,
                  progress REAL NOT NULL DEFAULT 0,
                  mode TEXT NOT NULL DEFAULT 'standard',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  workspace TEXT NOT NULL,
                  error TEXT,
                  engine_state TEXT,
                  model_provider TEXT NOT NULL DEFAULT 'manual',
                  model_name TEXT,
                  visual_model_provider TEXT NOT NULL DEFAULT 'manual',
                  visual_model_name TEXT,
                  products_json TEXT NOT NULL DEFAULT '[]',
                  materials_json TEXT NOT NULL DEFAULT '[]',
                  colors_json TEXT NOT NULL DEFAULT '[]',
                  subtitle_path TEXT,
                  delivery_mode TEXT NOT NULL DEFAULT 'merged',
                  creative_strategy TEXT NOT NULL DEFAULT 'auto',
                  target_min_seconds INTEGER NOT NULL DEFAULT 0,
                  target_max_seconds INTEGER NOT NULL DEFAULT 0,
                  token_input INTEGER NOT NULL DEFAULT 0,
                  token_output INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stages (
                  job_id TEXT NOT NULL,
                  stage_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  progress REAL NOT NULL DEFAULT 0,
                  message TEXT NOT NULL DEFAULT '',
                  started_at TEXT,
                  finished_at TEXT,
                  result_json TEXT,
                  error TEXT,
                  PRIMARY KEY (job_id, stage_id),
                  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL,
                  stage_id TEXT,
                  level TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  message TEXT NOT NULL,
                  payload_json TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  stage_id TEXT,
                  kind TEXT NOT NULL,
                  title TEXT NOT NULL,
                  path TEXT NOT NULL,
                  mime_type TEXT,
                  size INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  UNIQUE(job_id, path),
                  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                  key TEXT PRIMARY KEY,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
            if "model_provider" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN model_provider TEXT NOT NULL DEFAULT 'manual'")
            if "visual_model_provider" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN visual_model_provider TEXT NOT NULL DEFAULT 'manual'")
            if "visual_model_name" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN visual_model_name TEXT")
            if "products_json" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN products_json TEXT NOT NULL DEFAULT '[]'")
            if "materials_json" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN materials_json TEXT NOT NULL DEFAULT '[]'")
            if "colors_json" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN colors_json TEXT NOT NULL DEFAULT '[]'")
            if "subtitle_path" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN subtitle_path TEXT")
            if "delivery_mode" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'merged'")
            if "creative_strategy" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN creative_strategy TEXT NOT NULL DEFAULT 'auto'")
            if "target_min_seconds" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN target_min_seconds INTEGER NOT NULL DEFAULT 0")
            if "target_max_seconds" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN target_max_seconds INTEGER NOT NULL DEFAULT 0")
            con.execute("UPDATE jobs SET current_stage='validation' "
                        "WHERE current_stage IN ('rough_cut','pre_render_review')")
            con.execute("UPDATE events SET stage_id='validation' "
                        "WHERE stage_id IN ('rough_cut','pre_render_review')")
            con.execute("UPDATE artifacts SET stage_id='validation' "
                        "WHERE stage_id IN ('rough_cut','pre_render_review')")
            con.execute("DELETE FROM stages WHERE stage_id IN ('rough_cut','pre_render_review')")
            con.execute("UPDATE stages SET name='文本校验与原声锁定', position=65 "
                        "WHERE stage_id='validation'")
            con.execute("UPDATE stages SET name='AI 文本编排', position=40 "
                        "WHERE stage_id='edit_plan'")
            con.execute("UPDATE stages SET name='一次高清渲染' "
                        "WHERE stage_id='delivery'")
            con.execute("INSERT OR IGNORE INTO stages(job_id,stage_id,name,position) "
                        "SELECT id,'visual_mix','多模态画面混剪',80 FROM jobs")
            con.execute("UPDATE stages SET status='succeeded',progress=1,"
                        "message='旧任务在升级前已完成' WHERE stage_id='visual_mix' "
                        "AND job_id IN (SELECT id FROM jobs WHERE status='completed')")

    def create_job(self, *, title: str, source_path: str, brief: str, mode: str,
                   workspace: str, model_provider: str = "manual",
                   model_name: str | None = None, visual_model_provider: str = "manual",
                   visual_model_name: str | None = None, products: list[str] | None = None,
                   materials: list[str] | None = None, colors: list[str] | None = None,
                   subtitle_path: str | None = None,
                   delivery_mode: str = "merged", creative_strategy: str = "auto",
                   target_min_seconds: int = 0, target_max_seconds: int = 0) -> str:
        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO jobs(id,title,source_path,brief,status,current_stage,progress,mode,created_at,updated_at,workspace,model_provider,model_name,visual_model_provider,visual_model_name,products_json,materials_json,colors_json,subtitle_path,delivery_mode,creative_strategy,target_min_seconds,target_max_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, title, source_path, brief, "queued", "material_index", 0, mode, now, now,
                 workspace, model_provider, model_name, visual_model_provider, visual_model_name,
                 _json(products or []), _json(materials or []), _json(colors or []),
                 subtitle_path, delivery_mode, creative_strategy, target_min_seconds, target_max_seconds),
            )
            con.executemany(
                "INSERT INTO stages(job_id,stage_id,name,position) VALUES(?,?,?,?)",
                [(job_id, stage_id, name, position) for stage_id, name, position in STAGE_DEFINITIONS],
            )
        self.add_event(job_id, None, "info", "job_created", "任务已进入队列",
                       {"mode": mode, "model_provider": model_provider, "model_name": model_name,
                        "visual_model_provider": visual_model_provider,
                        "visual_model_name": visual_model_name, "products": products or [],
                        "materials": materials or [], "colors": colors or [],
                        "subtitle_path": subtitle_path, "delivery_mode": delivery_mode,
                        "creative_strategy": creative_strategy,
                        "target_min_seconds": target_min_seconds,
                        "target_max_seconds": target_max_seconds})
        return job_id

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_recoverable_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM jobs WHERE status IN "
                "('queued','running','waiting_input','failed') ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            stages = con.execute("SELECT * FROM stages WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
            events = con.execute("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 200", (job_id,)).fetchall()
            artifacts = con.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at DESC", (job_id,)).fetchall()
        job = dict(row)
        for field in ("products_json", "materials_json", "colors_json"):
            raw = job.pop(field, "[]")
            job[field.removesuffix("_json")] = json.loads(raw or "[]")
        job["stages"] = [self._decode_row(x, "result_json") for x in stages]
        job["events"] = [self._decode_row(x, "payload_json") for x in events]
        job["artifacts"] = [dict(x) for x in artifacts]
        return job

    @staticmethod
    def _decode_row(row: sqlite3.Row, field: str) -> dict[str, Any]:
        item = dict(row)
        raw = item.pop(field, None)
        item[field.removesuffix("_json")] = json.loads(raw) if raw else None
        return item

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        values = list(fields.values()) + [job_id]
        clause = ",".join(f"{key}=?" for key in fields)
        with self.connect() as con:
            con.execute(f"UPDATE jobs SET {clause} WHERE id=?", values)

    def touch_job(self, job_id: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE jobs SET updated_at=? WHERE id=?", (utc_now(), job_id))

    def delete_job(self, job_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return cur.rowcount > 0

    def reset_job(self, job_id: str) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET status='queued', current_stage='material_index', progress=0, "
                "error=NULL, started_at=NULL, finished_at=NULL, updated_at=?, engine_state=NULL, "
                "token_input=0, token_output=0 WHERE id=?",
                (now, job_id),
            )
            con.execute(
                "UPDATE stages SET status='pending', progress=0, message='', started_at=NULL, "
                "finished_at=NULL, result_json=NULL, error=NULL WHERE job_id=?",
                (job_id,),
            )
            con.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
        self.add_event(job_id, None, "info", "job_restarted", "任务已重置并重新开始执行")

    def update_stage(self, job_id: str, stage_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "result" in fields:
            fields["result_json"] = _json(fields.pop("result"))
        values = list(fields.values()) + [job_id, stage_id]
        clause = ",".join(f"{key}=?" for key in fields)
        with self.connect() as con:
            con.execute(f"UPDATE stages SET {clause} WHERE job_id=? AND stage_id=?", values)

    def stage_start(self, job_id: str, stage_id: str, message: str) -> None:
        now = utc_now()
        self.update_stage(job_id, stage_id, status="running", progress=0.03, message=message,
                          started_at=now, finished_at=None, error=None)
        self.update_job(job_id, status="running", current_stage=stage_id, started_at=now)
        self.add_event(job_id, stage_id, "info", "stage_started", message)

    def stage_done(self, job_id: str, stage_id: str, message: str,
                   result: dict[str, Any] | None = None) -> None:
        position = next((x[2] for x in STAGE_DEFINITIONS if x[0] == stage_id), 0)
        self.update_stage(job_id, stage_id, status="succeeded", progress=1, message=message,
                          finished_at=utc_now(), result=result)
        self.update_job(job_id, progress=position)
        self.add_event(job_id, stage_id, "success", "stage_completed", message, result)

    def stage_wait(self, job_id: str, stage_id: str, message: str,
                   result: dict[str, Any] | None = None) -> None:
        self.update_stage(job_id, stage_id, status="waiting_input", progress=0.65,
                          message=message, result=result)
        self.update_job(job_id, status="waiting_input", current_stage=stage_id)
        self.add_event(job_id, stage_id, "warning", "input_required", message, result)

    def stage_recover(self, job_id: str, stage_id: str, message: str,
                      result: dict[str, Any] | None = None) -> None:
        """Record a recoverable problem without creating a terminal failure state."""
        self.update_stage(job_id, stage_id, status="running", message=message, error=None,
                          finished_at=None, result=result)
        self.update_job(job_id, status="running", current_stage=stage_id, error=None,
                        finished_at=None)
        self.add_event(job_id, stage_id, "warning", "stage_recovering", message, result)

    def add_event(self, job_id: str, stage_id: str | None, level: str, kind: str,
                  message: str, payload: Any = None) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO events(job_id,stage_id,level,kind,message,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, stage_id, level, kind, message, _json(payload) if payload is not None else None, utc_now()),
            )

    def add_artifact(self, job_id: str, stage_id: str | None, kind: str, title: str,
                     path: Path, mime_type: str | None = None) -> str:
        path = Path(path).resolve()
        artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{job_id}:{path}").hex
        size = path.stat().st_size if path.exists() else 0
        with self.connect() as con:
            con.execute(
                "INSERT INTO artifacts(id,job_id,stage_id,kind,title,path,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id,path) DO UPDATE SET stage_id=excluded.stage_id,kind=excluded.kind,title=excluded.title,mime_type=excluded.mime_type,size=excluded.size",
                (artifact_id, job_id, stage_id, kind, title, str(path), mime_type, size, utc_now()),
            )
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def delete_artifacts(self, job_id: str, stage_ids: set[str]) -> None:
        """Remove stale artifact registrations when a review sends work upstream."""
        if not stage_ids:
            return
        placeholders = ",".join("?" for _ in stage_ids)
        with self.connect() as con:
            con.execute(
                f"DELETE FROM artifacts WHERE job_id=? AND stage_id IN ({placeholders})",
                [job_id, *sorted(stage_ids)],
            )

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, _json(value), utc_now()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as con:
            row = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def dashboard(self) -> dict[str, Any]:
        jobs = self.list_jobs()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        active = sum(counts.get(x, 0) for x in ("queued", "running", "waiting_input"))
        return {"jobs": jobs, "counts": counts, "active": active, "total": len(jobs)}
