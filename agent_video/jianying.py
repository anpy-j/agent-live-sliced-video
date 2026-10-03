from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
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


# 剪映的高版本 draft_content.json 是加密的，每个文件都要起一个子进程解密，单个约 0.5s。
# 草稿页此前每次请求都对全部草稿、全部时间线重新解密解析，几十个草稿要 40s+。
# 这里按「草稿内容指纹」缓存每个草稿的时间线摘要：内容不变就直接复用，只在冷启动时并行计算。
_DISCOVERY_WORKERS = 8
_DISCOVERY_CACHE_LIMIT = 2000
_discovery_cache: dict[str, dict[str, Any]] = {}
_discovery_lock = threading.Lock()
_discovery_cache_path: Path | None = None
_discovery_cache_dirty = False


def configure_discovery_cache(path: str | Path | None) -> None:
    """启用（或关闭）时间线摘要的磁盘缓存，使其在服务重启后依然有效。"""
    global _discovery_cache_path, _discovery_cache_dirty
    _discovery_cache_path = Path(path).expanduser() if path else None
    _discovery_cache_dirty = False
    _load_discovery_cache()


def _load_discovery_cache() -> None:
    path = _discovery_cache_path
    if path is None or not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    with _discovery_lock:
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(value.get("sig"), str):
                _discovery_cache.setdefault(key, value)


def _save_discovery_cache() -> None:
    global _discovery_cache_dirty
    path = _discovery_cache_path
    if path is None or not _discovery_cache_dirty:
        return
    with _discovery_lock:
        snapshot = json.dumps(_discovery_cache, ensure_ascii=False)
        _discovery_cache_dirty = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(snapshot, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _draft_content_signature(draft_path: Path, item: dict[str, Any]) -> str:
    """草稿内容指纹：任一 draft_content.json / 时间线布局变化都会让缓存失效。"""
    parts = [str(item.get("tm_draft_modified") or "")]
    for relative in ("timeline_layout.json", "draft_content.json"):
        try:
            stat = (draft_path / relative).stat()
        except OSError:
            continue
        parts.append(f"{relative}:{int(stat.st_mtime)}:{stat.st_size}")
    timelines_root = draft_path / "Timelines"
    if timelines_root.is_dir():
        try:
            children = sorted(timelines_root.iterdir(), key=lambda entry: entry.name)
        except OSError:
            children = []
        for child in children:
            content = child / "draft_content.json"
            try:
                stat = content.stat()
            except OSError:
                continue
            parts.append(f"timelines/{child.name}:{int(stat.st_mtime)}:{stat.st_size}")
    return "|".join(parts)


def _compute_timeline_summaries(draft_id: str, draft_path: Path, signature: str) -> None:
    global _discovery_cache_dirty
    from .timeline import discover_virtual_timelines

    timelines: list[dict[str, Any]] = []
    error: str | None = None
    try:
        discovered = discover_virtual_timelines(draft_path)
        timelines = discovered.get("timelines") or []
    except Exception as exc:  # noqa: BLE001 - 单个草稿失败只影响该卡片
        error = str(exc)
    with _discovery_lock:
        _discovery_cache[draft_id] = {"sig": signature, "timelines": timelines, "error": error}
        if len(_discovery_cache) > _DISCOVERY_CACHE_LIMIT:
            overflow = len(_discovery_cache) - _DISCOVERY_CACHE_LIMIT
            for stale in list(_discovery_cache)[:overflow]:
                _discovery_cache.pop(stale, None)
        _discovery_cache_dirty = True


def _cached_timeline_summaries(draft_id: str) -> dict[str, Any] | None:
    with _discovery_lock:
        return _discovery_cache.get(draft_id)


def list_jianying_drafts(
    root: Path | None = None,
    cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    if cache_path is not None:
        configure_discovery_cache(cache_path)

    draft_root = (root or default_draft_root()).resolve()
    prepared: list[tuple[dict[str, Any], Path, str, str]] = []
    for item in _load_registry(draft_root):
        draft_path = _resolve_draft_path(draft_root, item)
        if draft_path is None or not draft_path.is_dir():
            continue
        draft_id = hashlib.sha256(str(draft_path).casefold().encode("utf-8")).hexdigest()[:20]
        signature = _draft_content_signature(draft_path, item)
        prepared.append((item, draft_path, draft_id, signature))

    misses: list[tuple[str, Path, str]] = []
    for _, draft_path, draft_id, signature in prepared:
        cached = _cached_timeline_summaries(draft_id)
        if cached is None or cached.get("sig") != signature:
            misses.append((draft_id, draft_path, signature))
    if misses:
        with ThreadPoolExecutor(max_workers=min(_DISCOVERY_WORKERS, len(misses))) as pool:
            list(pool.map(lambda args: _compute_timeline_summaries(*args), misses))

    result: list[dict[str, Any]] = []
    for item, draft_path, draft_id, _ in prepared:
        name = str(item.get("draft_name") or draft_path.name).strip() or draft_path.name
        cover = _resolve_cover(draft_path, item)
        cached = _cached_timeline_summaries(draft_id) or {}
        timelines = cached.get("timelines") or []
        entry: dict[str, Any] = {
            "id": draft_id,
            "name": name,
            "path": str(draft_path),
            "modified_at": _modified_iso(item.get("tm_draft_modified"), draft_path),
            "cover_path": str(cover) if cover else None,
            "timelines": timelines,
            "timeline_count": len(timelines),
            "recommended_timeline": None,
        }
        if timelines:
            entry["recommended_timeline"] = max(
                timelines, key=lambda timeline: float(timeline.get("timeline_duration") or 0)
            )
        if cached.get("error"):
            entry["timeline_error"] = cached["error"]
        result.append(entry)
    result.sort(key=lambda draft: draft.get("modified_at") or "", reverse=True)
    _save_discovery_cache()
    return result


def draft_title_base(draft_name: str, now: datetime | None = None) -> str:
    clean_name = re.sub(r"[\\/:*?\"<>|]+", "", str(draft_name)).strip()
    if not clean_name:
        clean_name = "剪映草稿"
    return f"{clean_name}{(now or datetime.now()).strftime('%m%d')}"


def next_available_title(
    draft_name: str,
    existing_titles: Iterable[str],
    export_dir: str | Path | None = None,
    now: datetime | None = None,
) -> str:
    base = draft_title_base(draft_name, now)
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
