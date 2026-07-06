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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

from src.config.schema import DEFAULT_LOG_FILE
from src.config.web_config import (
    load_config_document,
    redacted_config,
    save_config_document,
)
from src.ocr_organizer import (
    DEFAULT_OCR_BACKFILL_RECENT_LIMIT,
    _load_history_rows,
    create_hardlink_view,
    load_recent_video_history_rows,
    select_ocr_backfill_message_ids,
)


CONFIG_FILE = Path(os.getenv("TDL_CONFIG_FILE", "/app/config.yaml"))
STATIC_DIR = Path(__file__).parent / "static"
MAX_LOG_LINES = 1000
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
        "ocr_map_auto_count": _json_item_count(auto_map),
        "ocr_review_queue_count": _json_item_count(review_queue),
        "ocr_verify_raw_count": _json_item_count(verify_raw),
        "backfill": backfill_status,
        "latest_auto_output_dirs": latest_auto_output_dirs,
        "recent_hardlink_view_folders": recent_views,
    }


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
