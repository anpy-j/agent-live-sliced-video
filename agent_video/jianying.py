from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXPORT_DIR = r"D:\切片\袁艺灵\AI粗筛视频"


def default_draft_root() -> Path:
    override = os.environ.get("JIANYING_DRAFT_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ValueError("未找到 LOCALAPPDATA，无法定位剪映草稿目录")
    return Path(local_app_data) / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"


def _load_registry(root: Path) -> list[dict[str, Any]]:
    registry = root / "root_meta_info.json"
    if not registry.is_file():
        raise ValueError(f"未找到剪映草稿索引: {registry}")
    data = json.loads(registry.read_text(encoding="utf-8-sig"))
    drafts = data.get("all_draft_store", []) if isinstance(data, dict) else []
    if isinstance(drafts, str):
        drafts = json.loads(drafts)
    if not isinstance(drafts, list):
        raise ValueError("剪映草稿索引中的 all_draft_store 格式无效")
    return [item for item in drafts if isinstance(item, dict)]


def _resolve_draft_path(root: Path, item: dict[str, Any]) -> Path | None:
    raw = str(item.get("draft_fold_path") or item.get("draft_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _resolve_cover(draft_path: Path, item: dict[str, Any]) -> Path | None:
    raw = str(item.get("draft_cover") or item.get("cover") or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    cover = Path(raw).expanduser()
    if not cover.is_absolute():
        cover = draft_path / cover
    try:
        cover = cover.resolve()
    except OSError:
        return None
    if cover != draft_path and draft_path not in cover.parents:
        return None
    return cover if cover.is_file() else None


def _modified_iso(value: Any, fallback: Path) -> str | None:
    try:
        stamp = float(value)
        while stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp).astimezone().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        try:
            return datetime.fromtimestamp(fallback.stat().st_mtime).astimezone().isoformat()
        except OSError:
            return None


def list_jianying_drafts(root: Path | None = None) -> list[dict[str, Any]]:
    from .timeline import discover_virtual_timelines

    draft_root = (root or default_draft_root()).resolve()
    result: list[dict[str, Any]] = []
    for item in _load_registry(draft_root):
        draft_path = _resolve_draft_path(draft_root, item)
        if draft_path is None or not draft_path.is_dir():
            continue
        name = str(item.get("draft_name") or draft_path.name).strip() or draft_path.name
        draft_id = hashlib.sha256(str(draft_path).casefold().encode("utf-8")).hexdigest()[:20]
        cover = _resolve_cover(draft_path, item)
        entry: dict[str, Any] = {
            "id": draft_id,
            "name": name,
            "path": str(draft_path),
            "modified_at": _modified_iso(item.get("tm_draft_modified"), draft_path),
            "cover_path": str(cover) if cover else None,
            "timelines": [],
            "timeline_count": 0,
            "recommended_timeline": None,
        }
        try:
            discovered = discover_virtual_timelines(draft_path)
            timelines = discovered.get("timelines") or []
            entry["timelines"] = timelines
            entry["timeline_count"] = len(timelines)
            if timelines:
                entry["recommended_timeline"] = max(
                    timelines, key=lambda timeline: float(timeline.get("timeline_duration") or 0)
                )
        except Exception as exc:
            entry["timeline_error"] = str(exc)
        result.append(entry)
    result.sort(key=lambda draft: draft.get("modified_at") or "", reverse=True)
    return result


def next_available_title(
    draft_name: str,
    existing_titles: Iterable[str],
    export_dir: str | Path | None = None,
    now: datetime | None = None,
) -> str:
    clean_name = re.sub(r"[\\/:*?\"<>|]+", "", str(draft_name)).strip()
    if not clean_name:
        clean_name = "剪映草稿"
    base = f"{clean_name}{(now or datetime.now()).strftime('%m%d')}"
    used = {str(title).casefold() for title in existing_titles}
    folder = Path(export_dir).expanduser() if export_dir else None

    def exists(candidate: str) -> bool:
        if candidate.casefold() in used or folder is None or not folder.is_dir():
            return candidate.casefold() in used
        return any((folder / f"{candidate}{suffix}").exists() for suffix in ("", ".mp4"))

    if not exists(base):
        return base
    suffix = 1
    while exists(f"{base}-{suffix}"):
        suffix += 1
    return f"{base}-{suffix}"
