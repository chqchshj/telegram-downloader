import json
import sqlite3

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.web.app import TASKS, apply_map, files_summary, index, ocr_plan, ocr_status


def _write_config(path, download_dir, session_dir):
    path.write_text(
        "\n".join(
            [
                "api_id: 12345",
                "api_hash: test_hash",
                f"download_dir: {download_dir}",
                f"session_dir: {session_dir}",
                "sources:",
                "  - url: https://t.me/example",
                "ocr_organizer:",
                "  enabled: true",
                f"  output_dir: {download_dir.parent / 'downloads_ocr'}",
                f"  cover_cache_dir: {session_dir / 'ocr_covers'}",
                "  min_confidence: 0.8",
            ]
        ),
        encoding="utf-8",
    )


def _state_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE download_history (
                file_unique_id TEXT,
                file_name TEXT,
                file_size INTEGER,
                source_key TEXT,
                message_id INTEGER,
                downloaded_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


@pytest.mark.asyncio
async def test_index_serves_login_shell_when_web_token_is_configured(monkeypatch):
    monkeypatch.setenv("TDL_WEB_TOKEN", "secret")

    html = await index()

    assert "登录控制台" in html
    assert "本地直连进入" in html


@pytest.mark.asyncio
async def test_files_summary_reports_counts_and_latest_files(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    config_path = temp_dir / "config.yaml"
    download_dir.mkdir()
    (download_dir / "one.mp4").write_bytes(b"111")
    (download_dir / "two.jpg").write_bytes(b"22")
    view = temp_dir / "downloads_ocr"
    view.mkdir()
    (view / "_hardlink_plan.json").write_text("[]", encoding="utf-8")
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    data = await files_summary(_request())

    assert data["download_dir"] == str(download_dir)
    assert data["total_size"] == 5
    assert data["counts_by_extension"] == {".jpg": 1, ".mp4": 1}
    assert str(view) in data["ocr_output_folders"]
    assert len(data["latest_files"]) == 2


@pytest.mark.asyncio
async def test_ocr_status_counts_manifests_and_candidates(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    cover_dir = session_dir / "ocr_covers" / "1619"
    cover_dir.mkdir(parents=True)
    (cover_dir / "manifest.json").write_text(
        json.dumps({"message_id": 1619, "candidates": [{"path": "a"}, {"path": "b"}]}),
        encoding="utf-8",
    )
    _state_db(session_dir / "state.db")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    data = await ocr_status(_request())

    assert data["cover_manifests"] == 1
    assert data["cover_candidates"] == 2
    assert data["paths"]["state_db_exists"] is True


@pytest.mark.asyncio
async def test_apply_map_rejects_output_inside_download_dir(temp_dir, monkeypatch):
    TASKS.clear()
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    download_dir.mkdir()
    _state_db(session_dir / "state.db")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    from src.web.app import ApplyMapRequest

    with pytest.raises(HTTPException) as exc:
        await apply_map(
            ApplyMapRequest(
                ocr_map_json={"1": {"title": "Title", "confidence": 1}},
                output_root=str(download_dir / "ocr"),
            ),
            _request(),
        )

    assert exc.value.status_code == 400
    assert "separate" in exc.value.detail


@pytest.mark.asyncio
async def test_ocr_plan_returns_compact_counts(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    plan_dir = temp_dir / "downloads_ocr"
    plan_dir.mkdir()
    (plan_dir / "_hardlink_plan.json").write_text(json.dumps([{"message_id": 1}]), encoding="utf-8")
    (plan_dir / "_skipped.json").write_text(json.dumps([{"message_id": 2}, {"message_id": 3}]), encoding="utf-8")
    (plan_dir / "_missing.json").write_text("[]", encoding="utf-8")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    data = await ocr_plan(str(plan_dir), _request())

    assert data["plan"]["count"] == 1
    assert data["skipped"]["count"] == 2
    assert data["missing"]["count"] == 0
