"""FastAPI control panel for telegram-downloader."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, ValidationError

from src.config.schema import DEFAULT_LOG_FILE
from src.config.web_config import (
    load_config_document,
    redacted_config,
    save_config_document,
)
from src.ocr_organizer import (
    DEFAULT_OCR_BACKFILL_RECENT_LIMIT,
    OCR_MAP_AUTO_FILE,
    OCR_REVIEW_QUEUE_FILE,
    OCR_VERIFY_RAW_FILE,
    _load_history_rows,
    create_hardlink_view,
    load_recent_video_history_rows,
    select_ocr_backfill_message_ids,
)


CONFIG_FILE = Path(os.getenv("TDL_CONFIG_FILE", "/app/config.yaml"))
STATIC_DIR = Path(__file__).parent / "static"
MAX_LOG_LINES = 1000
MAX_OCR_REVIEW_ITEMS = 30
MAX_OCR_CORRECTABLE_ITEMS = 80
OCR_CORRECTIONS_FILE = "ocr_corrections.json"
OCR_COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PLAN_FILES = {
    "plan": "_hardlink_plan.json",
    "skipped": "_skipped.json",
    "missing": "_missing.json",
}

app = FastAPI(title="Telegram Downloader Web Config", version="1.0.0")
TASKS: dict[str, dict[str, Any]] = {}


class ExtractCoversRequest(BaseModel):
    source_index: int | None = Field(default=None, ge=0)
    min_message_id: int | None = Field(default=None, ge=0)
    output_dir: str | None = None


class ApplyMapRequest(BaseModel):
    ocr_map_json: dict[str, Any] | None = None
    ocr_map_path: str | None = None
    cover_root: str | None = None
    output_root: str
    source_folder: str | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    dry_run: bool = False


class ReviewApplyRequest(BaseModel):
    title: str | None = None
    episode: str | int | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CorrectionApplyRequest(BaseModel):
    title: str
    episode: str | int
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


def _config_path() -> Path:
    return Path(os.getenv("TDL_CONFIG_FILE", str(CONFIG_FILE)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_config_path(config: dict[str, Any], value: str | Path | None, default: str | Path) -> Path:
    """Resolve relative paths against the current config file directory."""
    raw = Path(value or default).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (_config_path().parent / raw).resolve()


def _download_dir(config: dict[str, Any]) -> Path:
    return _resolve_config_path(config, config.get("download_dir"), "/downloads")


def _session_dir(config: dict[str, Any]) -> Path:
    return _resolve_config_path(config, config.get("session_dir"), "/app/.sessions")


def _state_db(config: dict[str, Any]) -> Path:
    return _session_dir(config) / "state.db"


def _ocr_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("ocr_organizer", {}) if isinstance(config.get("ocr_organizer"), dict) else {}


def _safe_ocr_config(config: dict[str, Any]) -> dict[str, Any]:
    ocr = dict(_ocr_config(config))
    if ocr.get("llm_api_key"):
        ocr["llm_api_key"] = "[redacted]"
    return ocr


def _cover_cache_dir(config: dict[str, Any]) -> Path:
    ocr = _ocr_config(config)
    return _resolve_config_path(config, ocr.get("cover_cache_dir"), _session_dir(config) / "ocr_covers")


def _ocr_output_dir(config: dict[str, Any]) -> Path:
    ocr = _ocr_config(config)
    return _resolve_config_path(config, ocr.get("output_dir"), _download_dir(config).parent / "downloads_ocr")


def _ocr_backfill_recent_limit(config: dict[str, Any]) -> int:
    try:
        return int(_ocr_config(config).get("backfill_recent_limit") or DEFAULT_OCR_BACKFILL_RECENT_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_OCR_BACKFILL_RECENT_LIMIT


def _json_item_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def _safe_text(value: Any, max_length: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_length:
        return f"{text[:max_length - 3]}..."
    return text


def _safe_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, confidence))


def _coerce_review_message_id(value: Any) -> int | None:
    try:
        message_id = int(value)
    except (TypeError, ValueError):
        return None
    return message_id if message_id >= 0 else None


def _load_review_queue_rows(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
    elif isinstance(data, dict):
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("message_id", key)
            rows.append(row)
    return rows


def _read_json_or_http(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {path.name}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read {path.name}: {exc}") from exc


def _write_json_atomic(path: Path, data: Any) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write {path.name}: {exc}") from exc
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def _review_row_message_id(item: Any, fallback: Any = None) -> int | None:
    if isinstance(item, dict):
        message_id = _coerce_review_message_id(item.get("message_id"))
        if message_id is not None:
            return message_id
    return _coerce_review_message_id(fallback)


def _remove_review_queue_item(data: Any, message_id: int) -> tuple[Any, dict[str, Any] | None]:
    found: dict[str, Any] | None = None

    if isinstance(data, list):
        next_rows = []
        for item in data:
            if _review_row_message_id(item) == message_id:
                if found is None and isinstance(item, dict):
                    found = dict(item)
                continue
            next_rows.append(item)
        return next_rows, found

    if isinstance(data, dict):
        next_rows = {}
        for key, item in data.items():
            if _review_row_message_id(item, key) == message_id:
                if found is None and isinstance(item, dict):
                    found = dict(item)
                    found.setdefault("message_id", key)
                continue
            next_rows[key] = item
        return next_rows, found

    raise HTTPException(status_code=500, detail="OCR review queue must be a JSON list or object")


def _ocr_counts(cover_dir: Path) -> dict[str, int]:
    return {
        "ocr_map_auto_count": _json_item_count(cover_dir / OCR_MAP_AUTO_FILE),
        "ocr_review_queue_count": _json_item_count(cover_dir / OCR_REVIEW_QUEUE_FILE),
        "ocr_verify_raw_count": _json_item_count(cover_dir / OCR_VERIFY_RAW_FILE),
    }


def _message_id_sort_key(message_id: str) -> tuple[int, int | str]:
    try:
        return (0, int(message_id))
    except ValueError:
        return (1, message_id)


def _load_auto_map_for_update(path: Path) -> dict[str, Any]:
    data = _read_json_or_http(path, {})
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="OCR auto map must be a JSON object")
    return dict(data)


def _clean_required_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_cover_image_path(cover_dir: Path, message_id: int, filename: str) -> Path:
    if message_id < 0:
        raise HTTPException(status_code=404, detail="cover not found")
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="invalid cover filename")
    if Path(filename).suffix.lower() not in OCR_COVER_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="unsupported cover image type")

    cover_root = cover_dir.resolve()
    message_dir = (cover_dir / str(message_id)).resolve()
    if not _is_relative_to(message_dir, cover_root):
        raise HTTPException(status_code=404, detail="cover not found")
    cover_path = (message_dir / filename).resolve()
    if not _is_relative_to(cover_path, message_dir):
        raise HTTPException(status_code=400, detail="invalid cover path")
    if not cover_path.exists() or not cover_path.is_file():
        raise HTTPException(status_code=404, detail="cover not found")
    return cover_path


def _cover_filename_from_path(message_dir: Path, value: Any) -> str | None:
    text = _safe_text(value, 512)
    if not text:
        return None
    candidate = Path(text)
    if candidate.suffix.lower() not in OCR_COVER_IMAGE_EXTENSIONS:
        return None
    if not candidate.is_absolute():
        candidate = message_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.exists() and resolved.is_file() and _is_relative_to(resolved, message_dir.resolve()):
        return resolved.name
    return None


def _find_cover_filename(cover_dir: Path, message_id: int, row: dict[str, Any]) -> str | None:
    message_dir = cover_dir / str(message_id)
    if not message_dir.exists() or not message_dir.is_dir():
        return None
    if not _is_relative_to(message_dir.resolve(), cover_dir.resolve()):
        return None

    filename = _cover_filename_from_path(message_dir, row.get("cover_path"))
    if filename:
        return filename

    manifest_path = message_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    candidates = manifest.get("candidates", []) if isinstance(manifest, dict) else []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            filename = _cover_filename_from_path(message_dir, candidate.get("path"))
            if filename:
                return filename

    images = [
        item
        for item in message_dir.iterdir()
        if item.is_file() and item.suffix.lower() in OCR_COVER_IMAGE_EXTENSIONS
    ]
    images.sort(key=lambda item: (0 if item.name.startswith(("video_thumb", "group_photo")) else 1, item.name))
    return images[0].name if images else None


def _review_message_sort_key(row: dict[str, Any]) -> int:
    message_id = _coerce_review_message_id(row.get("message_id"))
    return message_id if message_id is not None else -1


def _review_items(
    cover_dir: Path,
    review_queue: Path,
    history_rows: list[Any] | None = None,
    limit: int = MAX_OCR_REVIEW_ITEMS,
) -> list[dict[str, Any]]:
    rows = _load_review_queue_rows(review_queue)
    rows.sort(key=_review_message_sort_key, reverse=True)
    history_by_message_id = {
        int(row.message_id): row
        for row in (history_rows or [])
        if getattr(row, "message_id", None) is not None
    }

    items: list[dict[str, Any]] = []
    for row in rows:
        message_id = _coerce_review_message_id(row.get("message_id"))
        if message_id is None:
            continue

        item: dict[str, Any] = {"message_id": message_id}
        for field in ("title", "episode", "review_reason", "reason", "status"):
            value = _safe_text(row.get(field))
            if value is not None:
                item[field] = value
        confidence = _safe_confidence(row.get("confidence"))
        if confidence is not None:
            item["confidence"] = confidence

        history = history_by_message_id.get(message_id)
        if history:
            file_name = _safe_text(getattr(history, "file_name", None), 260)
            if file_name:
                item["source_file_name"] = file_name
            file_size = getattr(history, "file_size", None)
            if isinstance(file_size, int) and file_size >= 0:
                item["source_file_size"] = file_size
            downloaded_at = _safe_text(getattr(history, "downloaded_at", None), 80)
            if downloaded_at:
                item["source_downloaded_at"] = downloaded_at

        cover_filename = _find_cover_filename(cover_dir, message_id, row)
        if cover_filename:
            item["cover_url"] = f"/api/ocr/covers/{message_id}/{quote(cover_filename)}"

        items.append(item)
        if len(items) >= limit:
            break
    return items


def _history_rows_by_message_id(history_rows: list[Any]) -> dict[int, Any]:
    return {
        int(row.message_id): row
        for row in history_rows
        if getattr(row, "message_id", None) is not None
    }


def _correction_history_path(cover_dir: Path) -> Path:
    return cover_dir / OCR_CORRECTIONS_FILE


def _load_correction_history(cover_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    data = _read_json_or_http(_correction_history_path(cover_dir), [])
    rows = data if isinstance(data, list) else []
    safe_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return safe_rows[:limit]


def _organized_target_for_message(output_dir: Path, message_id: int) -> str | None:
    if not output_dir.exists() or not output_dir.is_dir():
        return None
    suffix = f"_{message_id}"
    for item in output_dir.rglob("*"):
        if not item.is_file() or item.name in PLAN_FILES.values():
            continue
        if item.stem.endswith(suffix):
            try:
                return str(item.relative_to(output_dir))
            except ValueError:
                return item.name
    return None


def _remove_existing_outputs_for_message(output_dir: Path, message_id: int) -> list[str]:
    """Remove prior organized files for a message before applying a correction."""
    plan_path = output_dir / PLAN_FILES["plan"]
    try:
        plan_rows = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(plan_rows, list):
        return []

    removed: list[str] = []
    output_root = output_dir.resolve()
    for row in plan_rows:
        if not isinstance(row, dict) or _coerce_review_message_id(row.get("message_id")) != message_id:
            continue
        for raw_path in (row.get("target"), row.get("cover")):
            if not raw_path:
                continue
            candidate = Path(str(raw_path)).resolve()
            if not _is_relative_to(candidate, output_root):
                continue
            for companion in (candidate, candidate.with_suffix(".nfo")):
                if companion.exists() and companion.is_file():
                    companion.unlink()
                    removed.append(str(companion.relative_to(output_root)))
            parent = candidate.parent
            try:
                remaining = [item for item in parent.iterdir() if item.name != "tvshow.nfo"]
                if not remaining:
                    tvshow = parent / "tvshow.nfo"
                    if tvshow.exists():
                        tvshow.unlink()
                        removed.append(str(tvshow.relative_to(output_root)))
                    parent.rmdir()
                    removed.append(str(parent.relative_to(output_root)))
            except OSError:
                pass
    return removed


def _correctable_items(
    cover_dir: Path,
    output_dir: Path,
    history_rows: list[Any],
    limit: int = MAX_OCR_CORRECTABLE_ITEMS,
) -> list[dict[str, Any]]:
    auto_map = _read_json_or_http(cover_dir / OCR_MAP_AUTO_FILE, {})
    if not isinstance(auto_map, dict):
        raise HTTPException(status_code=500, detail="OCR auto map must be a JSON object")
    history_by_message_id = _history_rows_by_message_id(history_rows)

    items: list[dict[str, Any]] = []
    for raw_message_id, entry in sorted(auto_map.items(), key=lambda item: _message_id_sort_key(str(item[0])), reverse=True):
        message_id = _coerce_review_message_id(raw_message_id)
        if message_id is None or not isinstance(entry, dict):
            continue

        item: dict[str, Any] = {"message_id": message_id}
        for source_field, target_field in (
            ("title", "title"),
            ("episode", "episode"),
            ("source", "source"),
            ("status", "status"),
            ("updated_at", "updated_at"),
            ("corrected_at", "corrected_at"),
        ):
            value = _safe_text(entry.get(source_field))
            if value is not None:
                item[target_field] = value
        confidence = _safe_confidence(entry.get("confidence"))
        if confidence is not None:
            item["confidence"] = confidence

        history = history_by_message_id.get(message_id)
        if history:
            file_name = _safe_text(getattr(history, "file_name", None), 260)
            if file_name:
                item["source_file_name"] = file_name
            file_size = getattr(history, "file_size", None)
            if isinstance(file_size, int) and file_size >= 0:
                item["source_file_size"] = file_size
            downloaded_at = _safe_text(getattr(history, "downloaded_at", None), 80)
            if downloaded_at:
                item["source_downloaded_at"] = downloaded_at

        target = _organized_target_for_message(output_dir, message_id)
        if target:
            item["organized_path"] = target

        items.append(item)
        if len(items) >= limit:
            break
    return items


def _append_correction_history(cover_dir: Path, row: dict[str, Any], keep: int = 200) -> None:
    history_path = _correction_history_path(cover_dir)
    data = _read_json_or_http(history_path, [])
    rows = data if isinstance(data, list) else []
    next_rows = [dict(item) for item in rows if isinstance(item, dict)]
    next_rows.insert(0, row)
    _write_json_atomic(history_path, next_rows[:keep])


def _output_file_count(path: Path, *, exclude_legacy_auto_children: bool = False) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    count = 0
    for item in path.rglob("*"):
        if not item.is_file() or item.name in PLAN_FILES.values():
            continue
        if exclude_legacy_auto_children:
            try:
                relative = item.relative_to(path)
            except ValueError:
                continue
            if relative.parts and relative.parts[0].startswith("auto_"):
                continue
        count += 1
    return count


def _hardlink_output_summary(path: Path, *, exclude_legacy_auto_children: bool = False) -> dict[str, Any]:
    plan_path = path / PLAN_FILES["plan"]
    modified_path = plan_path if plan_path.exists() else path
    return {
        "path": str(path),
        "file_count": _output_file_count(path, exclude_legacy_auto_children=exclude_legacy_auto_children),
        "plan_count": _json_item_count(plan_path),
        "modified_at": datetime.fromtimestamp(modified_path.stat().st_mtime, timezone.utc).isoformat(),
    }


def _reject_output_inside_download(config: dict[str, Any], output_root: str | Path) -> Path:
    output = _resolve_config_path(config, output_root, output_root)
    download = _download_dir(config)
    try:
        if output == download or output.is_relative_to(download):
            raise HTTPException(status_code=400, detail="output_root must be separate from download_dir")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return output


def _safe_existing_dir(config: dict[str, Any], path_value: str | Path) -> Path:
    path = _resolve_config_path(config, path_value, path_value)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="folder does not exist")
    return path


def _file_rows(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.exists() else []
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "path": str(item),
            "name": item.name,
            "size": item.stat().st_size,
            "modified_at": datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(),
        }
        for item in files[:limit]
    ]


def _compact_json_file(path: Path, limit: int = 20) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "count": 0, "rows": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else [data]
    return {
        "path": str(path),
        "exists": True,
        "count": len(rows),
        "rows": rows[:limit],
    }


def _source_index_default(config: dict[str, Any]) -> int:
    sources = config.get("sources") or []
    if not sources:
        raise HTTPException(status_code=400, detail="No sources configured")
    return 0


def _min_message_id_default(config: dict[str, Any]) -> int:
    state = _read_state(config)
    cursor = state.get("cursor") or {}
    values = [int(v) for v in cursor.values() if isinstance(v, int) or str(v).isdigit()]
    return min(values) if values else 1


def _new_task(kind: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "id": task_id,
        "kind": kind,
        "status": "queued",
        "created_at": _now(),
        "finished_at": None,
        "output": "",
        "result": None,
        "error": None,
    }
    return task_id


async def _run_subprocess_task(task_id: str, command: list[str]) -> None:
    task = TASKS[task_id]
    task["status"] = "running"
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        task["output"] = stdout.decode("utf-8", errors="replace")
        if process.returncode == 0:
            task["status"] = "succeeded"
        else:
            task["status"] = "failed"
            task["error"] = f"Command exited with {process.returncode}"
    except Exception as exc:  # pragma: no cover - defensive task boundary
        task["status"] = "failed"
        task["error"] = str(exc)
    finally:
        task["finished_at"] = _now()


async def _run_apply_map_task(
    task_id: str,
    config: dict[str, Any],
    ocr_map: str | dict[str, Any],
    cover_root: str | None,
    output_root: Path,
    source_folder: str | None,
    min_confidence: float,
    dry_run: bool,
) -> None:
    task = TASKS[task_id]
    task["status"] = "running"
    try:
        rows = await asyncio.to_thread(_load_history_rows, _state_db(config))
        result = await asyncio.to_thread(
            create_hardlink_view,
            rows,
            ocr_map,
            cover_root,
            _download_dir(config),
            output_root,
            source_folder,
            min_confidence,
            dry_run,
        )
        task["result"] = result
        task["status"] = "succeeded"
    except Exception as exc:
        task["status"] = "failed"
        task["error"] = str(exc)
    finally:
        task["finished_at"] = _now()


def _require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Apply simple Bearer token auth when TDL_WEB_TOKEN is configured."""
    token = os.getenv("TDL_WEB_TOKEN")
    if not token:
        return

    expected = f"Bearer {token}"
    provided = authorization or ""
    query_token = request.query_params.get("token")
    if provided != expected and query_token != token:
        raise HTTPException(status_code=401, detail="Missing or invalid token")


def _tdl_env_overrides() -> list[str]:
    ignored = {"TDL_CONFIG_FILE", "TDL_WEB_TOKEN"}
    return sorted(k for k in os.environ if k.startswith("TDL_") and k not in ignored)


def _read_health(config: dict[str, Any]) -> dict[str, Any]:
    health_path = Path(
        os.getenv(
            "TDL_DAEMON_HEALTH_FILE",
            config.get("daemon", {}).get("health_file", "/app/health_status.txt"),
        )
    )
    if not health_path.exists():
        return {"path": str(health_path), "exists": False}

    lines = health_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {
        "path": str(health_path),
        "exists": True,
        "status": lines[0] if lines else None,
        "timestamp": lines[1] if len(lines) > 1 else None,
    }


def _read_state(config: dict[str, Any]) -> dict[str, Any]:
    db_path = _state_db(config)
    result: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "cursor": {},
        "pending_count": 0,
    }
    if not db_path.exists():
        return result

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "cursors" in tables:
                result["cursor"] = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT source_key, last_message_id FROM cursors"
                    ).fetchall()
                }
            if "pending_downloads" in tables:
                row = conn.execute(
                    "SELECT COUNT(*) FROM pending_downloads"
                ).fetchone()
                result["pending_count"] = row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _bounded_log_line_count(value: int | str | None, default: int = 200) -> int:
    try:
        count = int(value) if value is not None else default
    except (TypeError, ValueError):
        count = default
    return max(1, min(count, MAX_LOG_LINES))


def _log_path(config: dict[str, Any]) -> Path:
    configured = os.getenv("TDL_LOG_FILE") or config.get("log_file")
    return _resolve_config_path(config, configured, DEFAULT_LOG_FILE)


def _tail_log_lines(path: Path, max_lines: int) -> list[str]:
    chunks: list[bytes] = []
    newline_count = 0
    chunk_size = 8192

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-max_lines:]


def _read_log_document(config: dict[str, Any], max_lines: int | str | None = 200) -> dict[str, Any]:
    line_count = _bounded_log_line_count(max_lines)
    log_path = _log_path(config)
    result: dict[str, Any] = {
        "path": str(log_path),
        "exists": log_path.exists(),
        "lines": [],
    }
    if not log_path.exists():
        return result
    if not log_path.is_file():
        result["error"] = "log path is not a file"
        return result

    try:
        lines = _tail_log_lines(log_path, line_count)
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["lines"] = lines[-line_count:]
    return result


def _read_recent_logs(config: dict[str, Any], max_lines: int = 80) -> list[str]:
    return _read_log_document(config, max_lines)["lines"]


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
async def get_config(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config_path = _config_path()
    data = load_config_document(config_path)
    return {
        "config_path": str(config_path),
        "config": redacted_config(data),
        "env_overrides": _tdl_env_overrides(),
        "token_required": bool(os.getenv("TDL_WEB_TOKEN")),
    }


@app.post("/api/config")
async def post_config(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    try:
        payload = await request.json()
        saved = save_config_document(_config_path(), payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "config_path": str(_config_path()),
        "config": redacted_config(saved),
        "env_overrides": _tdl_env_overrides(),
    }


@app.get("/api/health")
async def health_check():
    """Unauthenticated lightweight endpoint for Docker healthchecks."""
    return {"ok": True}


@app.get("/api/status")
async def get_status(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    return {
        "health_status": _read_health(config),
        "state": _read_state(config),
        "recent_logs": _read_recent_logs(config),
    }


@app.get("/api/logs")
async def get_logs(
    request: Request,
    lines: int = 200,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    return _read_log_document(config, lines)


@app.get("/api/files/summary")
async def files_summary(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    download = _download_dir(config)

    total_size = 0
    counts: dict[str, int] = {}
    if download.exists():
        for item in download.rglob("*"):
            if not item.is_file():
                continue
            stat = item.stat()
            total_size += stat.st_size
            ext = item.suffix.lower() or "[none]"
            counts[ext] = counts.get(ext, 0) + 1

    sibling_folders = []
    parent = download.parent
    if parent.exists():
        for folder in parent.glob(f"{download.name}*"):
            if folder.is_dir() and folder != download and any((folder / name).exists() for name in PLAN_FILES.values()):
                sibling_folders.append(str(folder))

    return {
        "download_dir": str(download),
        "exists": download.exists(),
        "total_size": total_size,
        "counts_by_extension": dict(sorted(counts.items())),
        "latest_files": _file_rows(download, 30),
        "ocr_output_folders": sibling_folders,
    }


@app.get("/api/ocr/status")
async def ocr_status(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    cover_dir = _cover_cache_dir(config)
    output_dir = _ocr_output_dir(config)
    state_db = _state_db(config)
    manifests = list(cover_dir.glob("*/manifest.json")) if cover_dir.exists() else []
    candidate_count = 0
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            candidate_count += len(data.get("candidates", [])) if isinstance(data, dict) else 0
        except (OSError, json.JSONDecodeError):
            continue

    recent_views = []
    latest_auto_output_dirs = []
    if output_dir.parent.exists():
        candidates = [
            folder
            for folder in output_dir.parent.iterdir()
            if folder.is_dir() and any((folder / name).exists() for name in PLAN_FILES.values())
        ]
        candidates.sort(key=lambda folder: folder.stat().st_mtime, reverse=True)
        recent_views = [str(folder) for folder in candidates[:12]]
    if output_dir.exists():
        auto_candidates = [
            folder
            for folder in output_dir.iterdir()
            if folder.is_dir() and folder.name.startswith("auto_")
        ]
        auto_candidates.sort(key=lambda folder: folder.stat().st_mtime, reverse=True)
        latest_auto_output_dirs = [str(folder) for folder in auto_candidates[:12]]

    auto_map = cover_dir / "ocr_map_auto.json"
    review_queue = cover_dir / "ocr_review_queue.json"
    verify_raw = cover_dir / "ocr_verify_raw.json"
    accepted_count = _json_item_count(auto_map)
    review_count = _json_item_count(review_queue)
    raw_count = _json_item_count(verify_raw)
    try:
        history_rows = _load_history_rows(state_db) if state_db.exists() else []
    except Exception:
        history_rows = []
    review_items = _review_items(cover_dir, review_queue, history_rows)
    correctable_items = _correctable_items(cover_dir, output_dir, history_rows, limit=20)
    correction_history = _load_correction_history(cover_dir, limit=10)

    direct_output = None
    direct_plan = output_dir / PLAN_FILES["plan"]
    if direct_plan.exists():
        direct_output = _hardlink_output_summary(output_dir, exclude_legacy_auto_children=True)

    latest_legacy_auto_output = None
    if latest_auto_output_dirs:
        latest_path = Path(latest_auto_output_dirs[0])
        latest_legacy_auto_output = _hardlink_output_summary(latest_path)

    latest_auto_output = direct_output or latest_legacy_auto_output

    backfill_limit = _ocr_backfill_recent_limit(config)
    backfill_status: dict[str, Any] = {
        "recent_limit": backfill_limit,
        "candidate_count": 0,
        "selected_count": 0,
        "selected_message_ids": [],
        "skip_reasons": {},
    }
    if state_db.exists():
        try:
            rows = load_recent_video_history_rows(state_db, limit=backfill_limit)
            selection = select_ocr_backfill_message_ids(
                rows,
                cover_root=cover_dir,
                download_root=_download_dir(config),
                output_root=output_dir,
            )
            backfill_status.update({
                "candidate_count": selection.candidate_count,
                "selected_count": len(selection.selected_message_ids),
                "selected_message_ids": selection.selected_message_ids,
                "skip_reasons": selection.skipped,
            })
        except Exception as exc:
            backfill_status["error"] = str(exc)

    ocr_config = _ocr_config(config)
    auto_enabled = bool(ocr_config.get("enabled"))
    if not auto_enabled:
        next_action = "未开启 OCR 自动整理"
    elif review_count:
        next_action = f"有 {review_count} 条待复核"
    elif backfill_status.get("selected_count"):
        next_action = f"有 {backfill_status['selected_count']} 条等待自动处理"
    elif not ocr_config.get("llm_verify_enabled"):
        next_action = "封面提取可用，自动识别未开启"
    else:
        next_action = "自动运行中，无需手动操作"

    organized_count = 0
    if latest_auto_output:
        organized_count = int(latest_auto_output.get("plan_count") or latest_auto_output.get("file_count") or 0)

    return {
        "config": _safe_ocr_config(config),
        "cover_cache_dir": str(cover_dir),
        "output_dir": str(output_dir),
        "state_db": str(state_db),
        "paths": {
            "cover_cache_dir_exists": cover_dir.exists(),
            "output_dir_exists": output_dir.exists(),
            "state_db_exists": state_db.exists(),
        },
        "cover_manifests": len(manifests),
        "cover_candidates": candidate_count,
        "ocr_map_auto_exists": auto_map.exists(),
        "ocr_review_queue_exists": review_queue.exists(),
        "ocr_verify_raw_exists": verify_raw.exists(),
        "ocr_map_auto_count": accepted_count,
        "ocr_review_queue_count": review_count,
        "ocr_verify_raw_count": raw_count,
        "review_items": review_items,
        "correctable_items": correctable_items,
        "correction_history": correction_history,
        "auto_dashboard": {
            "enabled": auto_enabled,
            "llm_verify_enabled": bool(ocr_config.get("llm_verify_enabled")),
            "llm_auto_apply": bool(ocr_config.get("llm_auto_apply")),
            "state": "running" if auto_enabled else "disabled",
            "cover_candidates": candidate_count,
            "accepted_count": accepted_count,
            "review_count": review_count,
            "raw_count": raw_count,
            "organized_count": organized_count,
            "latest_output": latest_auto_output,
            "next_action": next_action,
        },
        "backfill": backfill_status,
        "latest_auto_output": latest_auto_output,
        "latest_auto_output_dirs": latest_auto_output_dirs,
        "recent_hardlink_view_folders": recent_views,
    }


@app.get("/api/ocr/corrections")
async def list_ocr_corrections(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    cover_dir = _cover_cache_dir(config)
    output_dir = _ocr_output_dir(config)
    state_db = _state_db(config)
    try:
        history_rows = _load_history_rows(state_db) if state_db.exists() else []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read download history: {exc}") from exc
    return {
        "ok": True,
        "output_dir": str(output_dir),
        "items": _correctable_items(cover_dir, output_dir, history_rows),
        "history": _load_correction_history(cover_dir),
        **_ocr_counts(cover_dir),
    }


@app.get("/api/ocr/organized-all")
async def list_organized_all(request: Request, authorization: str | None = Header(default=None)):
    """List ALL organized items from the hardlink plan, grouped by series title."""
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    output_dir = _ocr_output_dir(config)

    plan_path = output_dir / PLAN_FILES["plan"]
    if not plan_path.exists():
        return {"ok": True, "series": [], "total_series": 0, "total_files": 0}

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": True, "series": [], "total_series": 0, "total_files": 0}

    if not isinstance(plan_data, list):
        return {"ok": True, "series": [], "total_series": 0, "total_files": 0}

    # Group by title
    series_map: dict[str, list[dict[str, Any]]] = {}
    for item in plan_data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        series_map.setdefault(title, []).append(item)

    # Load OCR map to check which items are already correctable
    cover_dir = _cover_cache_dir(config)
    auto_map = _read_json_or_http(cover_dir / OCR_MAP_AUTO_FILE, {})

    series = []
    for title in sorted(series_map, key=str.casefold):
        items = series_map[title]
        series_items = []
        for item in items:
            mid = _coerce_review_message_id(item.get("message_id"))
            episode = item.get("episode")
            target_str = item.get("target")
            # Check if this message_id is already in OCR map (for correction)
            in_map = str(mid) in auto_map if mid is not None else False
            series_items.append({
                "message_id": mid,
                "episode": episode,
                "target": target_str,
                "source": item.get("source"),
                "cover": item.get("cover"),
                "in_ocr_map": in_map,
            })
        series.append({
            "title": title,
            "count": len(items),
            "items": series_items,
        })

    return {
        "ok": True,
        "series": series,
        "total_series": len(series),
        "total_files": sum(s["count"] for s in series),
    }


@app.post("/api/ocr/sync-plan-to-map")
async def sync_plan_to_map(request: Request, authorization: str | None = Header(default=None)):
    """Sync all organized items from the hardlink plan into ocr_map_auto.json so they become correctable."""
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    output_dir = _ocr_output_dir(config)
    cover_dir = _cover_cache_dir(config)

    plan_path = output_dir / PLAN_FILES["plan"]
    if not plan_path.exists():
        raise HTTPException(status_code=404, detail="No hardlink plan found")

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read plan: {exc}") from exc

    if not isinstance(plan_data, list):
        raise HTTPException(status_code=500, detail="Plan file must be a JSON array")

    auto_map_path = cover_dir / OCR_MAP_AUTO_FILE
    auto_map = _load_auto_map_for_update(auto_map_path)

    synced: list[int] = []
    for item in plan_data:
        if not isinstance(item, dict):
            continue
        mid = _coerce_review_message_id(item.get("message_id"))
        if mid is None:
            continue
        key = str(mid)
        if key in auto_map:
            continue  # Already in map, skip
        title = str(item.get("title") or "").strip()
        episode = item.get("episode")
        if not title:
            continue
        auto_map[key] = {
            "title": title,
            "episode": episode,
            "confidence": 1.0,
            "source": "plan_sync",
            "status": "synced",
            "synced_at": _now(),
            "updated_at": _now(),
        }
        synced.append(mid)

    if synced:
        ordered_auto_map = dict(sorted(auto_map.items(), key=lambda item: _message_id_sort_key(str(item[0]))))
        _write_json_atomic(auto_map_path, ordered_auto_map)

    return {
        "ok": True,
        "synced": len(synced),
        "synced_message_ids": synced,
        "total_in_map": len(auto_map),
        **_ocr_counts(cover_dir),
    }


@app.post("/api/ocr/corrections/{message_id}/apply")
async def apply_ocr_correction(
    message_id: int,
    payload: CorrectionApplyRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    if message_id < 0:
        raise HTTPException(status_code=400, detail="message_id must be non-negative")

    title = _clean_required_text(payload.title)
    episode = _clean_required_text(payload.episode)
    if title is None:
        raise HTTPException(status_code=400, detail="title is required for OCR correction")
    if episode is None:
        raise HTTPException(status_code=400, detail="episode is required for OCR correction")

    config = load_config_document(_config_path())
    cover_dir = _cover_cache_dir(config)
    output_dir = _reject_output_inside_download(config, _ocr_output_dir(config))
    state_db = _state_db(config)
    if not state_db.exists():
        raise HTTPException(status_code=400, detail=f"state_db does not exist: {state_db}")

    try:
        rows = _load_history_rows(state_db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read download history: {exc}") from exc
    if message_id not in _history_rows_by_message_id(rows):
        raise HTTPException(status_code=404, detail="message_id not found in download history")

    auto_map_path = cover_dir / OCR_MAP_AUTO_FILE
    auto_map = _load_auto_map_for_update(auto_map_path)
    previous = auto_map.get(str(message_id))
    if not isinstance(previous, dict):
        previous = {}

    confidence = payload.confidence if payload.confidence is not None else previous.get("confidence", 1.0)
    corrected_at = _now()
    auto_map[str(message_id)] = {
        **previous,
        "title": title,
        "episode": episode,
        "confidence": confidence,
        "source": "web_correction",
        "status": "corrected",
        "corrected_at": corrected_at,
        "updated_at": corrected_at,
    }
    ordered_auto_map = dict(sorted(auto_map.items(), key=lambda item: _message_id_sort_key(str(item[0]))))
    _write_json_atomic(auto_map_path, ordered_auto_map)
    removed_outputs = _remove_existing_outputs_for_message(output_dir, message_id)

    try:
        apply_result = create_hardlink_view(
            rows,
            ordered_auto_map,
            cover_dir,
            _download_dir(config),
            output_dir,
            None,
            0.0,
            False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR correction apply failed: {exc}") from exc

    history_row = {
        "message_id": message_id,
        "previous_title": _safe_text(previous.get("title")),
        "previous_episode": _safe_text(previous.get("episode")),
        "title": title,
        "episode": episode,
        "confidence": confidence,
        "corrected_at": corrected_at,
    }
    _append_correction_history(cover_dir, history_row)

    return {
        "ok": True,
        "status": "corrected",
        "message_id": message_id,
        "correction": history_row,
        "removed_outputs": removed_outputs,
        "apply": {"output_root": str(output_dir), **apply_result},
        "planned_count": apply_result.get("planned", 0),
        **_ocr_counts(cover_dir),
    }


@app.post("/api/ocr/review/{message_id}/ignore")
async def ignore_ocr_review(
    message_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    if message_id < 0:
        raise HTTPException(status_code=400, detail="message_id must be non-negative")

    config = load_config_document(_config_path())
    cover_dir = _cover_cache_dir(config)
    review_queue = cover_dir / OCR_REVIEW_QUEUE_FILE
    queue_data = _read_json_or_http(review_queue, [])
    next_queue, removed_row = _remove_review_queue_item(queue_data, message_id)
    _write_json_atomic(review_queue, next_queue)

    return {
        "ok": True,
        "status": "ignored",
        "message_id": message_id,
        "removed": removed_row is not None,
        **_ocr_counts(cover_dir),
    }


@app.post("/api/ocr/review/{message_id}/apply")
async def apply_ocr_review(
    message_id: int,
    payload: ReviewApplyRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    if message_id < 0:
        raise HTTPException(status_code=400, detail="message_id must be non-negative")

    config = load_config_document(_config_path())
    cover_dir = _cover_cache_dir(config)
    output_dir = _reject_output_inside_download(config, _ocr_output_dir(config))
    state_db = _state_db(config)
    if not state_db.exists():
        raise HTTPException(status_code=400, detail=f"state_db does not exist: {state_db}")

    try:
        rows = _load_history_rows(state_db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read download history: {exc}") from exc

    review_queue = cover_dir / OCR_REVIEW_QUEUE_FILE
    queue_data = _read_json_or_http(review_queue, [])
    next_queue, review_row = _remove_review_queue_item(queue_data, message_id)
    review_row = review_row or {}

    title = _clean_required_text(payload.title)
    if title is None:
        title = _clean_required_text(review_row.get("title"))
    episode = _clean_required_text(payload.episode)
    if episode is None:
        episode = _clean_required_text(review_row.get("episode"))
    if title is None:
        raise HTTPException(status_code=400, detail="title is required for OCR review apply")
    if episode is None:
        raise HTTPException(status_code=400, detail="episode is required for OCR review apply")

    confidence = payload.confidence if payload.confidence is not None else 1.0
    auto_map_path = cover_dir / OCR_MAP_AUTO_FILE
    auto_map = _load_auto_map_for_update(auto_map_path)
    auto_map[str(message_id)] = {
        "title": title,
        "episode": episode,
        "confidence": confidence,
        "source": "web_review",
        "status": "manual_applied",
        "updated_at": _now(),
    }
    ordered_auto_map = dict(sorted(auto_map.items(), key=lambda item: _message_id_sort_key(str(item[0]))))
    _write_json_atomic(auto_map_path, ordered_auto_map)

    try:
        apply_result = create_hardlink_view(
            rows,
            ordered_auto_map,
            cover_dir,
            _download_dir(config),
            output_dir,
            None,
            0.0,
            False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR review apply failed: {exc}") from exc

    _write_json_atomic(review_queue, next_queue)

    return {
        "ok": True,
        "status": "applied",
        "message_id": message_id,
        "review_removed": bool(review_row),
        "apply": {"output_root": str(output_dir), **apply_result},
        "planned_count": apply_result.get("planned", 0),
        **_ocr_counts(cover_dir),
    }


@app.get("/api/ocr/covers/{message_id}/{filename}")
async def ocr_cover(
    message_id: int,
    filename: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    cover_path = _safe_cover_image_path(_cover_cache_dir(config), message_id, filename)
    return FileResponse(cover_path)


@app.post("/api/ocr/extract-covers")
async def extract_covers(
    payload: ExtractCoversRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    output_dir = _resolve_config_path(config, payload.output_dir, _cover_cache_dir(config))
    output_dir.mkdir(parents=True, exist_ok=True)
    source_index = payload.source_index if payload.source_index is not None else _source_index_default(config)
    min_message_id = payload.min_message_id if payload.min_message_id is not None else _min_message_id_default(config)

    task_id = _new_task("ocr.extract-covers")
    command = [
        sys.executable,
        "-m",
        "src.ocr_organizer",
        "extract-covers",
        "--config",
        str(_config_path()),
        "--source-index",
        str(source_index),
        "--min-message-id",
        str(min_message_id),
        "--output-dir",
        str(output_dir),
    ]
    TASKS[task_id]["command"] = " ".join(command)
    asyncio.create_task(_run_subprocess_task(task_id, command))
    return TASKS[task_id]


@app.post("/api/ocr/apply-map")
async def apply_map(
    payload: ApplyMapRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    if payload.ocr_map_json is None and not payload.ocr_map_path:
        raise HTTPException(status_code=400, detail="Provide ocr_map_json or ocr_map_path")

    output_root = _reject_output_inside_download(config, payload.output_root)
    state_db = _state_db(config)
    if not state_db.exists():
        raise HTTPException(status_code=400, detail=f"state_db does not exist: {state_db}")

    ocr_map: str | dict[str, Any]
    if payload.ocr_map_json is not None:
        maps_dir = _session_dir(config) / "ocr_maps"
        maps_dir.mkdir(parents=True, exist_ok=True)
        map_path = maps_dir / f"web-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        map_path.write_text(json.dumps(payload.ocr_map_json, ensure_ascii=False, indent=2), encoding="utf-8")
        ocr_map = str(map_path)
    else:
        ocr_map = str(_resolve_config_path(config, payload.ocr_map_path, payload.ocr_map_path))

    cover_root = str(_resolve_config_path(config, payload.cover_root, _cover_cache_dir(config))) if payload.cover_root else None
    min_confidence = payload.min_confidence
    if min_confidence is None:
        min_confidence = float(_ocr_config(config).get("min_confidence") or 0.0)

    task_id = _new_task("ocr.apply-map")
    asyncio.create_task(
        _run_apply_map_task(
            task_id,
            config,
            ocr_map,
            cover_root,
            output_root,
            payload.source_folder,
            min_confidence,
            payload.dry_run,
        )
    )
    TASKS[task_id]["input"] = {
        "ocr_map_path": ocr_map if isinstance(ocr_map, str) else None,
        "cover_root": cover_root,
        "output_root": str(output_root),
        "source_folder": payload.source_folder,
        "min_confidence": min_confidence,
        "dry_run": payload.dry_run,
    }
    return TASKS[task_id]


@app.get("/api/tasks")
async def list_tasks(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    return {"tasks": sorted(TASKS.values(), key=lambda task: task["created_at"], reverse=True)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    return TASKS[task_id]


@app.get("/api/ocr/plan")
async def ocr_plan(path: str, request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    folder = _safe_existing_dir(config, path)
    return {
        name: _compact_json_file(folder / filename)
        for name, filename in PLAN_FILES.items()
    }


@app.post("/api/restart")
async def restart(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    return {
        "ok": False,
        "message": "Configuration saved. Restart the downloader container to apply process-level changes.",
    }
