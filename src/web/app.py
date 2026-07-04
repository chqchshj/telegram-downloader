"""Minimal FastAPI configuration panel for telegram-downloader."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from src.config.web_config import (
    load_config_document,
    redacted_config,
    save_config_document,
)


CONFIG_FILE = Path(os.getenv("TDL_CONFIG_FILE", "/app/config.yaml"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Telegram Downloader Web Config", version="1.0.0")


def _config_path() -> Path:
    return Path(os.getenv("TDL_CONFIG_FILE", str(CONFIG_FILE)))


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
    session_dir = Path(config.get("session_dir", "/app/.sessions"))
    db_path = session_dir / "state.db"
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


def _read_recent_logs(config: dict[str, Any], max_lines: int = 80) -> list[str]:
    log_file = config.get("log_file")
    if not log_file:
        return []

    log_path = Path(log_file)
    if not log_path.exists():
        return []

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
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


@app.get("/api/status")
async def get_status(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    config = load_config_document(_config_path())
    return {
        "health_status": _read_health(config),
        "state": _read_state(config),
        "recent_logs": _read_recent_logs(config),
    }


@app.post("/api/restart")
async def restart(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    return {
        "ok": False,
        "message": "Configuration saved. Restart the downloader container to apply process-level changes.",
    }
