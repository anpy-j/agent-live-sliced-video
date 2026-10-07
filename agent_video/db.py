from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


# 精简管线的唯一阶段序列：ASR → 规则筛 → AI 判定 → AI 排序 → 渲染。
STAGE_DEFINITIONS = [
    ("asr", "语音转写与切分", 20),
    ("filter", "规则粗筛", 40),
    ("judge", "AI 可用性判定", 60),
    ("order", "AI 精选编排", 75),
    ("review", "AI 正片文本筛查", 90),
    ("render", "渲染成片", 100),
]

_LEAN_STAGE_IDS = {stage_id for stage_id, _, _ in STAGE_DEFINITIONS}

# 旧管线遗留在 jobs 表里的独占列；出现即代表磁盘上是旧 schema。
_LEGACY_JOB_COLUMNS = {
    "brief", "mode", "engine_state", "model_name", "token_input", "token_output",
    "model_provider", "visual_model_provider", "visual_model_name", "products_json",
    "materials_json", "colors_json", "subtitle_path", "delivery_mode",
    "creative_strategy", "target_min_seconds", "target_max_seconds", "semantic_engine",
}


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

    def _archive_legacy_store(self) -> None:
        """旧管线的 jobs/stages 与精简 schema 不兼容，归档后重建，避免旧字段泄漏。

        只重命名数据库文件（保留为 agent.db.legacy-<时间戳>），不删除任何产物。
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            con = sqlite3.connect(self.path, timeout=5)
        except sqlite3.Error:
            return
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" not in tables:
                return
            columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            stage_ids = ({row[0] for row in con.execute("SELECT DISTINCT stage_id FROM stages")}
                         if "stages" in tables else set())
        except sqlite3.Error:
            return
        finally:
            con.close()
        if not (columns & _LEGACY_JOB_COLUMNS or stage_ids - _LEAN_STAGE_IDS):
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = self.path.with_name(f"{self.path.name}.legacy-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{self.path}{suffix}")
            if source.exists():
                source.rename(Path(f"{archive}{suffix}"))
        print(f"[db] 检测到旧管线数据库，已归档为 {archive.name} 并重建精简 schema",
              file=sys.stderr)

    def init(self) -> None:
        self._archive_legacy_store()
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  source_path TEXT NOT NULL,
                  status TEXT NOT NULL,
                  current_stage TEXT,
                  progress REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  workspace TEXT NOT NULL,
                  error TEXT,
                  job_type TEXT NOT NULL DEFAULT 'direct',
                  timeline_meta_json TEXT,
                  target_seconds TEXT DEFAULT '70-90',
                  run_stage TEXT
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
                CREATE TABLE IF NOT EXISTS label_sessions (
                  id TEXT PRIMARY KEY,
                  source_path TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'ready',
                  clauses_json TEXT NOT NULL DEFAULT '[]',
                  decisions_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            if "job_type" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'direct'")
            if "timeline_meta_json" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN timeline_meta_json TEXT")
            if "target_seconds" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN target_seconds TEXT DEFAULT '70-90'")
            if "run_stage" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN run_stage TEXT")
            if "export_dir" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN export_dir TEXT")
            if "product_name" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN product_name TEXT")
            if "export_mode" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN export_mode TEXT DEFAULT 'merge'")
            if "edit_count" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN edit_count INTEGER NOT NULL DEFAULT 0")
            if "delivered" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
            # Add S5 in place: preserve historic jobs, artifacts and completed videos.
            con.execute("INSERT OR IGNORE INTO stages(job_id,stage_id,name,position,status) "
                        "SELECT id,'review','AI 正片文本筛查',90,"
                        "CASE WHEN status='completed' THEN 'succeeded' "
                        "ELSE 'pending' END FROM jobs WHERE job_type!='remix'")
            con.execute("UPDATE stages SET position=75 WHERE stage_id='order'")

    def create_job(self, *, title: str, source_path: str, workspace: str,
                   job_type: str = "direct", timeline_meta: dict[str, Any] | None = None,
                   target_seconds: str = "70-90",
                   export_dir: str | None = None,
                   product_name: str | None = None,
                   export_mode: str = "merge") -> str:
        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = utc_now()
        first_stage = STAGE_DEFINITIONS[0][0]
        meta_json = _json(timeline_meta) if timeline_meta else None
        with self.connect() as con:
            con.execute(
                "INSERT INTO jobs(id,title,source_path,status,current_stage,progress,created_at,updated_at,workspace,"
                "job_type,timeline_meta_json,target_seconds,export_dir,product_name,export_mode) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, title, source_path, "queued", first_stage, 0, now, now, workspace,
                 job_type, meta_json, target_seconds, export_dir, product_name, export_mode),
            )
            con.executemany(
                "INSERT INTO stages(job_id,stage_id,name,position) VALUES(?,?,?,?)",
                [(job_id, stage_id, name, position) for stage_id, name, position in STAGE_DEFINITIONS
                 if not (job_type == "remix" and stage_id == "review")],
            )
        event_payload: dict[str, Any] = {
            "source_path": source_path,
            "job_type": job_type,
            "target_seconds": target_seconds,
        }
        if timeline_meta:
            event_payload["timeline_id"] = timeline_meta.get("timeline_id")
            event_payload["duration"] = timeline_meta.get("total_duration")
            event_payload["segments"] = len(timeline_meta.get("segments") or [])
        self.add_event(job_id, None, "info", "job_created", "任务已进入队列", event_payload)
        return job_id

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        jobs = []
        for row in rows:
            j = dict(row)
            raw_meta = j.pop("timeline_meta_json", None)
            j["timeline_meta"] = json.loads(raw_meta) if raw_meta else None
            jobs.append(j)
        return jobs

    def list_job_titles(self) -> list[str]:
        with self.connect() as con:
            rows = con.execute("SELECT title FROM jobs").fetchall()
        return [str(row["title"]) for row in rows]

    def list_active_job_titles(self) -> list[str]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT title FROM jobs WHERE status IN ('queued','running','waiting_input')"
            ).fetchall()
        return [str(row["title"]) for row in rows]

    def list_recoverable_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
        jobs = []
        for row in rows:
            j = dict(row)
            raw_meta = j.pop("timeline_meta_json", None)
            j["timeline_meta"] = json.loads(raw_meta) if raw_meta else None
            jobs.append(j)
        return jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            stages = con.execute("SELECT * FROM stages WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
            events = con.execute("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 200", (job_id,)).fetchall()
            artifacts = con.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at DESC", (job_id,)).fetchall()
        job = dict(row)
        raw_meta = job.pop("timeline_meta_json", None)
        job["timeline_meta"] = json.loads(raw_meta) if raw_meta else None
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

    def bump_edit_count(self, job_id: str) -> None:
        """一次成片产出 = 剪辑一次；并清掉上一轮的「已剪辑」标记。"""
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET edit_count=edit_count+1, delivered=0, updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )

    def mark_delivered(self, job_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("UPDATE jobs SET delivered=1, updated_at=? WHERE id=?",
                              (utc_now(), job_id))
            return cur.rowcount > 0

    def delete_job(self, job_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return cur.rowcount > 0

    def reset_job(self, job_id: str) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET status='queued', current_stage=?, progress=0, run_stage=NULL, "
                "error=NULL, started_at=NULL, finished_at=NULL, delivered=0, updated_at=? "
                "WHERE id=?",
                (STAGE_DEFINITIONS[0][0], now, job_id),
            )
            con.execute(
                "UPDATE stages SET status='pending', progress=0, message='', started_at=NULL, "
                "finished_at=NULL, result_json=NULL, error=NULL WHERE job_id=?",
                (job_id,),
            )
            con.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
        self.add_event(job_id, None, "info", "job_restarted", "任务已重置并重新开始执行")

    def prepare_stage_rerun(self, job_id: str, stage_id: str) -> None:
        """重置目标节点及其下游状态，同时保留上游状态和历史渲染产物。"""
        positions = {sid: position for sid, _, position in STAGE_DEFINITIONS}
        if stage_id not in {"filter", "judge", "order", "review", "render"}:
            raise ValueError(f"不支持单节点重跑：{stage_id}")
        position = positions[stage_id]
        previous_progress = max((pos for pos in positions.values() if pos < position), default=0)
        downstream = [sid for sid, pos in positions.items() if pos >= position]
        placeholders = ",".join("?" for _ in downstream)
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET status='queued', current_stage=?, progress=?, run_stage=?, "
                "error=NULL, started_at=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                (stage_id, previous_progress, stage_id, now, job_id),
            )
            con.execute(
                f"UPDATE stages SET status='pending', progress=0, message='', started_at=NULL, "
                f"finished_at=NULL, result_json=NULL, error=NULL "
                f"WHERE job_id=? AND stage_id IN ({placeholders})",
                (job_id, *downstream),
            )
            # 历史成片必须保留；仅移除将被重算的 JSON 节点登记。
            cleared_artifacts = [sid for sid in downstream if sid != "render"]
            if cleared_artifacts:
                artifact_marks = ",".join("?" for _ in cleared_artifacts)
                con.execute(
                    f"DELETE FROM artifacts WHERE job_id=? AND stage_id IN ({artifact_marks})",
                    (job_id, *cleared_artifacts),
                )
        self.add_event(job_id, stage_id, "info", "stage_requeued",
                       f"{stage_id} 已加入单节点重跑队列")

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
        self.update_job(job_id, status="running", current_stage=stage_id, progress=0,
                        started_at=now)
        self.add_event(job_id, stage_id, "info", "stage_started", message)

    def stage_done(self, job_id: str, stage_id: str, message: str,
                   result: dict[str, Any] | None = None) -> None:
        position = next((x[2] for x in STAGE_DEFINITIONS if x[0] == stage_id), 0)
        self.update_stage(job_id, stage_id, status="succeeded", progress=1, message=message,
                          finished_at=utc_now(), result=result, error=None)
        self.update_job(job_id, progress=position)
        self.add_event(job_id, stage_id, "success", "stage_completed", message, result)

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

    def create_label_session(self, *, source_path: str) -> str:
        session_id = f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO label_sessions(id,source_path,status,clauses_json,decisions_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (session_id, source_path, "ready", "[]", "{}", now, now),
            )
        return session_id

    def set_label_clauses(self, session_id: str, clauses: list[dict[str, Any]],
                          status: str = "ready") -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE label_sessions SET clauses_json=?, status=?, updated_at=? WHERE id=?",
                (_json(clauses), status, utc_now(), session_id),
            )

    def set_label_decisions(self, session_id: str, decisions: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE label_sessions SET decisions_json=?, updated_at=? WHERE id=?",
                (_json(decisions), utc_now(), session_id),
            )

    @staticmethod
    def _decode_label(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["clauses"] = json.loads(item.pop("clauses_json") or "[]")
        item["decisions"] = json.loads(item.pop("decisions_json") or "{}")
        return item

    def get_label_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM label_sessions WHERE id=?", (session_id,)).fetchone()
        return self._decode_label(row) if row else None

    def list_label_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM label_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_label(row) for row in rows]

    def delete_label_session(self, session_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("DELETE FROM label_sessions WHERE id=?", (session_id,))
            return cur.rowcount > 0

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
        active = sum(counts.get(x, 0) for x in ("queued", "running"))
        return {"jobs": jobs, "counts": counts, "active": active, "total": len(jobs)}
