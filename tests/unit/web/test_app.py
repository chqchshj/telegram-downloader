import json
import sqlite3

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.web.app import (
    TASKS,
    apply_map,
    files_summary,
    get_logs,
    get_status,
    index,
    ocr_cover,
    ocr_plan,
    ocr_status,
)


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
async def test_logs_endpoint_uses_runtime_default_when_config_omits_log_file(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))
    monkeypatch.delenv("TDL_LOG_FILE", raising=False)

    data = await get_logs(_request())

    assert data["path"] == "/app/runtime/downloader.log"
    assert isinstance(data["exists"], bool)
    assert isinstance(data["lines"], list)


@pytest.mark.asyncio
async def test_logs_endpoint_reads_configured_log_file_and_caps_lines(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    log_file = temp_dir / "runtime" / "downloader.log"
    log_file.parent.mkdir()
    log_file.write_text("\n".join(f"line {idx}" for idx in range(1105)), encoding="utf-8")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\nlog_file: {log_file}\n")
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))
    monkeypatch.delenv("TDL_LOG_FILE", raising=False)

    data = await get_logs(_request(), lines=5000)
    status = await get_status(_request())

    assert data["path"] == str(log_file)
    assert data["exists"] is True
    assert len(data["lines"]) == 1000
    assert data["lines"][0] == "line 105"
    assert data["lines"][-1] == "line 1104"
    assert status["recent_logs"] == [f"line {idx}" for idx in range(1025, 1105)]


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
    (session_dir / "ocr_covers" / "ocr_map_auto.json").write_text(
        json.dumps({"1619": {"title": "Title"}}),
        encoding="utf-8",
    )
    (session_dir / "ocr_covers" / "ocr_review_queue.json").write_text(
        json.dumps([{"message_id": 1620}]),
        encoding="utf-8",
    )
    (session_dir / "ocr_covers" / "ocr_verify_raw.json").write_text(
        json.dumps([{"message_id": 1619}, {"message_id": 1620}]),
        encoding="utf-8",
    )
    auto_dir = download_dir.parent / "downloads_ocr" / "auto_20260706_120000"
    auto_dir.mkdir(parents=True)
    _state_db(session_dir / "state.db")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n  llm_api_key: secret-token\n")
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    data = await ocr_status(_request())

    assert data["cover_manifests"] == 1
    assert data["cover_candidates"] == 2
    assert data["paths"]["state_db_exists"] is True
    assert data["ocr_map_auto_count"] == 1
    assert data["ocr_review_queue_count"] == 1
    assert data["ocr_verify_raw_count"] == 2
    assert str(auto_dir) in data["latest_auto_output_dirs"]
    assert data["latest_auto_output"]["path"] == str(auto_dir)
    assert data["auto_dashboard"]["enabled"] is True
    assert data["auto_dashboard"]["accepted_count"] == 1
    assert data["auto_dashboard"]["review_count"] == 1
    assert data["auto_dashboard"]["next_action"] == "有 1 条待复核"
    assert data["config"]["llm_api_key"] == "[redacted]"


@pytest.mark.asyncio
async def test_ocr_status_includes_capped_safe_review_items(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    cover_root = session_dir / "ocr_covers"
    message_dir = cover_root / "35"
    message_dir.mkdir(parents=True)
    (message_dir / "video_thumb_1.jpg").write_bytes(b"image")
    review_rows = [
        {
            "message_id": message_id,
            "title": f"Title {message_id}",
            "episode": str(message_id),
            "confidence": 0.51,
            "review_reason": "low_confidence",
            "status": "model_ok",
            "reason": "needs review",
            "cover_path": "/secret/cover.jpg",
            "api_key": "secret-token",
        }
        for message_id in range(1, 36)
    ]
    (cover_root / "ocr_review_queue.json").write_text(
        json.dumps(review_rows),
        encoding="utf-8",
    )
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    data = await ocr_status(_request())

    assert data["ocr_review_queue_count"] == 35
    assert len(data["review_items"]) == 30
    assert data["review_items"][0] == {
        "message_id": 35,
        "title": "Title 35",
        "episode": "35",
        "review_reason": "low_confidence",
        "reason": "needs review",
        "status": "model_ok",
        "confidence": 0.51,
        "cover_url": "/api/ocr/covers/35/video_thumb_1.jpg",
    }
    assert data["review_items"][-1]["message_id"] == 6
    serialized = json.dumps(data["review_items"], ensure_ascii=False)
    assert "secret-token" not in serialized
    assert "cover_path" not in serialized


@pytest.mark.asyncio
async def test_ocr_cover_endpoint_rejects_unsafe_paths_and_non_images(temp_dir, monkeypatch):
    download_dir = temp_dir / "downloads"
    session_dir = temp_dir / "sessions"
    cover_dir = session_dir / "ocr_covers" / "99"
    cover_dir.mkdir(parents=True)
    image = cover_dir / "video_thumb_1.jpg"
    image.write_bytes(b"image")
    (cover_dir / "notes.txt").write_text("not an image", encoding="utf-8")
    config_path = temp_dir / "config.yaml"
    _write_config(config_path, download_dir, session_dir)
    monkeypatch.setenv("TDL_CONFIG_FILE", str(config_path))

    response = await ocr_cover(99, "video_thumb_1.jpg", _request())

    assert response.path == image
    with pytest.raises(HTTPException) as traversal:
        await ocr_cover(99, "../secret.jpg", _request())
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException) as non_image:
        await ocr_cover(99, "notes.txt", _request())
    assert non_image.value.status_code == 400


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
