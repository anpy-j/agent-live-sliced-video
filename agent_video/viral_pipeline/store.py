from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


V2_STAGES = (
    ("prepare", "素材准备", 10),
    ("profile", "素材画像", 25),
    ("retrieve", "爆款检索", 40),
    ("blueprint", "剪辑蓝图", 55),
    ("map", "素材映射", 70),
    ("review", "成片复审", 85),
    ("render", "渲染交付", 100),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ViralStore:
    """Storage boundary for V2. It never reads or writes the legacy tables."""

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
                CREATE TABLE IF NOT EXISTS viral_references (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  transcript TEXT NOT NULL,
                  likes INTEGER,
                  duration_seconds REAL,
                  published_at TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS viral_reference_analyses (
                  reference_id TEXT NOT NULL,
                  analyzer_version TEXT NOT NULL,
                  model TEXT NOT NULL,
                  dna_json TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  estimated_cost REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(reference_id, analyzer_version),
                  FOREIGN KEY(reference_id) REFERENCES viral_references(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS viral_strategy_clusters (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  prototype_json TEXT NOT NULL,
                  reference_ids_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS viral_jobs (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  source_kind TEXT NOT NULL,
                  source_path TEXT,
                  source_text TEXT,
                  reference_ids_json TEXT NOT NULL DEFAULT '[]',
                  target_seconds TEXT NOT NULL,
                  reference_mode TEXT NOT NULL DEFAULT 'hybrid',
                  status TEXT NOT NULL DEFAULT 'draft',
                  current_stage TEXT,
                  progress REAL NOT NULL DEFAULT 0,
                  workspace TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS viral_job_stages (
                  job_id TEXT NOT NULL,
                  stage_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  result_json TEXT,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  estimated_cost REAL NOT NULL DEFAULT 0,
                  error TEXT,
                  PRIMARY KEY(job_id, stage_id),
                  FOREIGN KEY(job_id) REFERENCES viral_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS viral_job_outputs (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  path TEXT NOT NULL,
                  metadata_json TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(job_id) REFERENCES viral_jobs(id) ON DELETE CASCADE
                );
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(viral_jobs)")}
            if "reference_ids_json" not in columns:
                con.execute(
                    "ALTER TABLE viral_jobs ADD COLUMN reference_ids_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )

    def create_reference(self, *, title: str, transcript: str,
                         likes: int | None = None,
                         duration_seconds: float | None = None,
                         published_at: str | None = None) -> dict[str, Any]:
        reference_id = f"vr_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO viral_references(id,title,transcript,likes,duration_seconds,"
                "published_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (reference_id, title, transcript, likes, duration_seconds,
                 published_at, "pending", now, now),
            )
        return self.get_reference(reference_id) or {"id": reference_id}

    def list_references(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT r.*, a.analyzer_version, a.model, a.dna_json, a.input_tokens,"
                "a.output_tokens, a.estimated_cost FROM viral_references r "
                "LEFT JOIN viral_reference_analyses a ON a.rowid=("
                "SELECT a2.rowid FROM viral_reference_analyses a2 "
                "WHERE a2.reference_id=r.id ORDER BY a2.created_at DESC,a2.rowid DESC LIMIT 1) "
                "ORDER BY r.created_at DESC"
            ).fetchall()
        return [self._decode_reference(row) for row in rows]

    def get_reference(self, reference_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT r.*, a.analyzer_version, a.model, a.dna_json, a.input_tokens,"
                "a.output_tokens, a.estimated_cost FROM viral_references r "
                "LEFT JOIN viral_reference_analyses a ON a.reference_id=r.id "
                "WHERE r.id=? ORDER BY a.created_at DESC LIMIT 1", (reference_id,)
            ).fetchone()
        return self._decode_reference(row) if row else None

    @staticmethod
    def _decode_reference(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        raw = value.pop("dna_json", None)
        value["dna"] = json.loads(raw) if raw else None
        return value

    def save_analysis(self, reference_id: str, *, analyzer_version: str,
                      model: str, dna: dict[str, Any], input_tokens: int = 0,
                      output_tokens: int = 0, estimated_cost: float = 0) -> None:
        now = utc_now()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM viral_references WHERE id=?",
                                 (reference_id,)).fetchone()
            if not exists:
                raise KeyError("爆款样本不存在")
            con.execute(
                "INSERT OR REPLACE INTO viral_reference_analyses("
                "reference_id,analyzer_version,model,dna_json,input_tokens,output_tokens,"
                "estimated_cost,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference_id, analyzer_version, model, _json(dna), input_tokens,
                 output_tokens, estimated_cost, now),
            )
            con.execute(
                "UPDATE viral_references SET status='ready',error=NULL,updated_at=? WHERE id=?",
                (now, reference_id),
            )

    def mark_reference_failed(self, reference_id: str, error: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE viral_references SET status='failed',error=?,updated_at=? WHERE id=?",
                (error[:2000], utc_now(), reference_id),
            )

    def delete_reference(self, reference_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("DELETE FROM viral_references WHERE id=?", (reference_id,))
        return cur.rowcount > 0

    def create_job(self, *, title: str, source_kind: str, workspace: str,
                   target_seconds: str, source_path: str | None = None,
                   source_text: str | None = None,
                   reference_mode: str = "hybrid",
                   reference_ids: list[str] | None = None) -> dict[str, Any]:
        job_id = f"viral_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO viral_jobs(id,title,source_kind,source_path,source_text,"
                "reference_ids_json,target_seconds,reference_mode,status,current_stage,workspace,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, title, source_kind, source_path, source_text,
                 _json(reference_ids or []), target_seconds, reference_mode, "draft",
                 V2_STAGES[0][0], workspace, now, now),
            )
            con.executemany(
                "INSERT INTO viral_job_stages(job_id,stage_id,name,position) VALUES(?,?,?,?)",
                [(job_id, stage_id, name, position)
                 for stage_id, name, position in V2_STAGES],
            )
        return self.get_job(job_id) or {"id": job_id}

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM viral_jobs ORDER BY created_at DESC").fetchall()
            return [self._decode_job_row(dict(row), con=con) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM viral_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            stages = con.execute(
                "SELECT * FROM viral_job_stages WHERE job_id=? ORDER BY position", (job_id,)
            ).fetchall()
            result = self._decode_job_row(dict(row), con=con)
        result["stages"] = []
        for stage in stages:
            item = dict(stage)
            raw = item.pop("result_json", None)
            item["result"] = json.loads(raw) if raw else None
            result["stages"].append(item)
        return result

    def _decode_job_row(self, result: dict[str, Any],
                        con: sqlite3.Connection | None) -> dict[str, Any]:
        try:
            reference_ids = json.loads(result.pop("reference_ids_json", "[]") or "[]")
        except (TypeError, json.JSONDecodeError):
            reference_ids = []
        result["reference_ids"] = reference_ids
        if con is None or not reference_ids:
            result["references"] = []
            return result
        placeholders = ",".join("?" for _ in reference_ids)
        rows = con.execute(
            f"SELECT id,title,status FROM viral_references WHERE id IN ({placeholders})",
            reference_ids,
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        result["references"] = [by_id[item] for item in reference_ids if item in by_id]
        return result
