# -*- coding: utf-8 -*-
"""Versioned, bounded feedback rules for text, vision, and rendering."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RULES = [
    "多商品按商品集中，同一商品内按颜色集中；商品优先级高于颜色。",
    "近义重复必须删除；面料内容在完整成片中最多出现一次。",
    "价格、优惠、库存、催单和尺码等违规信息不得进入成片。",
]
SCOPES = {"text", "vision", "render"}
KINDS = {"hard", "soft", "one_time"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(text: str) -> str:
    normalized = re.sub(r"[\W_]+", "", text.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class DirectivesManager:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.rules_file = self.root / "data" / "rules" / "directing_rules.json"
        self.history_dir = self.root / "data" / "rules" / "history"

    def load(self) -> dict[str, Any]:
        if self.rules_file.is_file():
            try:
                data = json.loads(self.rules_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("rules", [])
                    data.setdefault("learned_directives", [])  # legacy API compatibility
                    return data
            except Exception:
                pass
        return {"version": 1, "global_rules": list(DEFAULT_RULES), "rules": [],
                "learned_directives": [], "updated_at": _now()}

    def save(self, data: dict[str, Any]) -> None:
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        if self.rules_file.is_file():
            self.history_dir.mkdir(parents=True, exist_ok=True)
            old = self.load()
            try:
                revision = int(old.get("version") or 1)
            except (TypeError, ValueError):
                revision = 1
            (self.history_dir / f"v{revision:04d}.json").write_text(
                json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            current_version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            current_version = 1
        data["version"] = current_version + 1
        data["updated_at"] = _now()
        temporary = self.rules_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.rules_file)

    def add_feedback(self, *, problem_type: str, task: str, expected_change: str,
                     scope: str = "text", kind: str = "soft", job_id: str | None = None,
                     timestamp: str | None = None, segment: str | None = None,
                     product: str | None = None, verified: bool = False) -> dict[str, Any]:
        if scope not in SCOPES or kind not in KINDS:
            raise ValueError("scope/kind 不合法")
        text = expected_change.strip()
        if not text:
            raise ValueError("期望修改不能为空")
        if kind == "hard" and not verified:
            raise ValueError("硬规则全局生效前必须通过确定性校验或回归测试")
        current = self.load()
        identity = _key("|".join((scope, kind, problem_type.strip(), text)))
        existing = next((item for item in current["rules"] if item.get("id") == identity), None)
        if existing:
            return current
        contradictory = []
        polarity = any(word in text for word in ("不要", "禁止", "不得", "删除"))
        for item in current["rules"]:
            if item.get("scope") != scope or item.get("problem_type") != problem_type:
                continue
            other = str(item.get("expected_change", ""))
            if polarity != any(word in other for word in ("不要", "禁止", "不得", "删除")):
                contradictory.append(item.get("id"))
        entry = {"id": identity, "enabled": not contradictory, "kind": kind,
                 "scope": scope, "problem_type": problem_type.strip(), "task": task.strip(),
                 "timestamp": timestamp, "segment": segment, "product": product,
                 "expected_change": text, "job_id": job_id, "verified": bool(verified),
                 "conflicts_with": contradictory, "created_at": _now()}
        current["rules"].append(entry)
        current["learned_directives"] = [
            {"directive": item["expected_change"], "job_id": item.get("job_id")}
            for item in current["rules"] if item.get("enabled")]
        self.save(current)
        return current

    def add_learned_directive(self, directive: str,
                              source_job_id: str | None = None) -> dict[str, Any]:
        return self.add_feedback(problem_type="general", task="video_edit",
                                 expected_change=directive, scope="text", kind="soft",
                                 job_id=source_job_id)

    def set_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        data = self.load()
        item = next((rule for rule in data["rules"] if rule.get("id") == rule_id), None)
        if not item:
            raise KeyError(rule_id)
        item["enabled"] = bool(enabled)
        self.save(data)
        return data

    def rollback(self, version: int) -> dict[str, Any]:
        path = self.history_dir / f"v{int(version):04d}.json"
        if not path.is_file():
            raise ValueError(f"规则版本不存在: {version}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.save(data)
        return self.load()

    def get_prompt_rules(self, scope: str = "text", budget: int = 1200) -> list[str]:
        data = self.load()
        values = list(data.get("global_rules", [])) if scope == "text" else []
        for item in data.get("rules", []):
            if not item.get("enabled", True) or item.get("scope") != scope:
                continue
            if item.get("kind") == "one_time":
                continue
            values.append(str(item.get("expected_change", "")))
        result, used = [], 0
        for value in values:
            if not value or used + len(value) > budget:
                break
            result.append(value)
            used += len(value)
        return result
