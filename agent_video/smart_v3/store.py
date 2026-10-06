from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("S1", "S2", "S3", "S4", "S5")


class SmartStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS smart_jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self, job):
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO smart_jobs VALUES (?,?)",
                        (job["id"], json.dumps(job, ensure_ascii=False, allow_nan=False)))
        return job

    def create(self, payload):
        return self.save({"id": str(uuid.uuid4()), "version": "V3", "status": "draft",
                          "input": payload, "stages": {s: {"status": "pending"} for s in STAGES},
                          "artifacts": [], "attempt": 0, "error": None,
                          "feedback": {"decisions": [], "order": [], "deletion_reasons": {},
                                       "human_retained_seconds": None, "retention_ratio": None}})

    def get(self, job_id):
        with self.connect() as con:
            row = con.execute("SELECT body FROM smart_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("V3 job not found")
        return json.loads(row[0])

    def list(self):
        with self.connect() as con:
            rows = con.execute("SELECT body FROM smart_jobs ORDER BY rowid DESC").fetchall()
        return {"jobs": [json.loads(row[0]) for row in rows]}

    def delete(self, job_id):
        self.get(job_id)
        with self.connect() as con:
            con.execute("DELETE FROM smart_jobs WHERE id=?", (job_id,))
