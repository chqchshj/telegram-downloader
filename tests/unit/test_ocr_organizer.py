"""Tests for the OCR-map hardlink organizer."""
import json
import os

import pytest

from src.ocr_organizer import (
    _parse_llm_json_content,
    build_ocr_filename,
    build_parser,
    create_hardlink_view,
    normalize_episode_suffix,
    resolve_downloaded_media_path,
    verify_cover_manifests,
)


def test_episode_normalization():
    assert normalize_episode_suffix("EP-1") == "EP01"
    assert normalize_episode_suffix("第12集") == "EP12"
    assert normalize_episode_suffix(3) == "EP03"


def test_build_ocr_filename_sanitizes_components():
    folder, filename = build_ocr_filename("美丽/新世界", "EP-1", 1619, ".mp4")

    assert folder == "美丽_新世界"
    assert filename == "美丽_新世界_EP01_1619.mp4"


def test_resolve_downloaded_media_path_uses_sanitizer(temp_dir):
    source_dir = temp_dir / "AI短剧"
    source_dir.mkdir()
    media = source_dir / "美丽新世界_EP-1.mp4"
    media.write_bytes(b"video")

    resolved = resolve_downloaded_media_path(
        temp_dir,
        "AI短剧",
        "美丽新世界 EP-1.mp4",
        5,
    )

    assert resolved == media


def test_resolve_downloaded_media_path_handles_conflict_by_size(temp_dir):
    source_dir = temp_dir / "AI短剧"
    source_dir.mkdir()
    (source_dir / "episode.mp4").write_bytes(b"old")
    conflicted = source_dir / "episode_2.mp4"
    conflicted.write_bytes(b"new-video")

    resolved = resolve_downloaded_media_path(temp_dir, "AI短剧", "episode.mp4", 9)

    assert resolved == conflicted


def test_create_hardlink_view_creates_separate_links_and_preserves_source(temp_dir):
    download_root = temp_dir / "downloads"
    output_root = temp_dir / "downloads_ocr"
    source_dir = download_root / "AI短剧"
    source_dir.mkdir(parents=True)
    source = source_dir / "episode.mp4"
    source.write_bytes(b"video")

    result = create_hardlink_view(
        [
            {
                "file_unique_id": "uid",
                "file_name": "episode.mp4",
                "file_size": 5,
                "source_key": "channel:1",
                "message_id": 1619,
            }
        ],
        {"1619": {"title": "美丽新世界", "episode": "1", "confidence": 0.99}},
        {},
        download_root,
        output_root,
        min_confidence=0.8,
    )

    target = output_root / "美丽新世界" / "美丽新世界_EP01_1619.mp4"
    assert result["planned"] == 1
    assert target.exists()
    assert source.exists()
    assert os.stat(source).st_ino == os.stat(target).st_ino


def test_create_hardlink_view_skips_low_confidence_and_no_title(temp_dir):
    download_root = temp_dir / "downloads"
    output_root = temp_dir / "downloads_ocr"
    download_root.mkdir()
    (download_root / "a.mp4").write_bytes(b"a")
    (download_root / "b.mp4").write_bytes(b"b")

    result = create_hardlink_view(
        [
            {"file_unique_id": "a", "file_name": "a.mp4", "file_size": 1, "source_key": "s", "message_id": 1},
            {"file_unique_id": "b", "file_name": "b.mp4", "file_size": 1, "source_key": "s", "message_id": 2},
        ],
        {
            "1": {"title": "", "episode": "1", "confidence": 0.99},
            "2": {"title": "Title", "episode": "2", "confidence": 0.2},
        },
        {},
        download_root,
        output_root,
        min_confidence=0.8,
    )

    assert result["planned"] == 0
    assert result["skipped"] == 2


def test_create_hardlink_view_reports_missing_source(temp_dir):
    result = create_hardlink_view(
        [
            {
                "file_unique_id": "uid",
                "file_name": "missing.mp4",
                "file_size": 12,
                "source_key": "s",
                "message_id": 10,
            }
        ],
        {"10": {"title": "Title", "episode": "1", "confidence": 1.0}},
        {},
        temp_dir / "downloads",
        temp_dir / "downloads_ocr",
    )

    assert result["missing"] == 1


def test_create_hardlink_view_adds_suffix_for_target_collisions(temp_dir):
    download_root = temp_dir / "downloads"
    output_root = temp_dir / "downloads_ocr"
    download_root.mkdir()
    (download_root / "one.mp4").write_bytes(b"one")
    existing_dir = output_root / "Same"
    existing_dir.mkdir(parents=True)
    (existing_dir / "Same_EP01_1.mp4").write_bytes(b"existing")

    result = create_hardlink_view(
        [
            {"file_unique_id": "one", "file_name": "one.mp4", "file_size": 3, "source_key": "s", "message_id": 1},
        ],
        {
            "1": {"title": "Same", "episode": "1", "confidence": 1.0},
        },
        {},
        download_root,
        output_root,
        dry_run=True,
    )

    plan = (output_root / "_hardlink_plan.json").read_text(encoding="utf-8")
    assert result["planned"] == 1
    assert "Same_EP01_1_2.mp4" in plan


def test_create_hardlink_view_rejects_output_inside_download_root(temp_dir):
    download_root = temp_dir / "downloads"
    download_root.mkdir()

    with pytest.raises(ValueError):
        create_hardlink_view([], {}, {}, download_root, download_root / "ocr")


def test_parse_llm_json_content_handles_fenced_json():
    parsed = _parse_llm_json_content(
        '```json\n{"title":"Title","episode":"1","is_drama":true,"confidence":0.95}\n```'
    )

    assert parsed["title"] == "Title"
    assert parsed["confidence"] == 0.95


def test_verify_cover_manifests_accepts_only_conservative_rows(temp_dir, monkeypatch):
    cover_root = temp_dir / "covers"
    for message_id in (1, 2, 3):
        item_dir = cover_root / str(message_id)
        item_dir.mkdir(parents=True)
        cover = item_dir / "group_photo_1.jpg"
        cover.write_bytes(b"image")
        (item_dir / "manifest.json").write_text(
            json.dumps({
                "message_id": message_id,
                "candidates": [{"path": str(cover), "preferred": True}],
            }),
            encoding="utf-8",
        )

    def fake_call(cover_path, *, message_id, base_url, api_key, model, timeout=60):
        rows = {
            1: {"title": "Good", "episode": "1", "is_drama": True, "confidence": 0.96},
            2: {"title": "Low", "episode": "2", "is_drama": True, "confidence": 0.6},
            3: {"title": "Ad", "episode": "3", "is_drama": False, "confidence": 0.99},
        }
        return {
            "message_id": message_id,
            "reason": "",
            "cover_path": str(cover_path),
            "status": "model_ok",
            **rows[message_id],
        }

    monkeypatch.setattr("src.ocr_organizer.call_llm_vision_endpoint", fake_call)

    result = verify_cover_manifests(cover_root, cover_root, min_confidence=0.92)

    assert result["accepted"] == 1
    assert result["review"] == 2
    auto_map = json.loads((cover_root / "ocr_map_auto.json").read_text(encoding="utf-8"))
    review = json.loads((cover_root / "ocr_review_queue.json").read_text(encoding="utf-8"))
    assert list(auto_map) == ["1"]
    assert {row["review_reason"] for row in review} == {"low_confidence", "not_drama"}


def test_verify_cover_manifests_can_allow_non_drama_and_missing_episode(temp_dir, monkeypatch):
    cover_dir = temp_dir / "covers" / "9"
    cover_dir.mkdir(parents=True)
    cover = cover_dir / "cover.jpg"
    cover.write_bytes(b"image")
    (cover_dir / "manifest.json").write_text(
        json.dumps({"message_id": 9, "candidates": [{"path": str(cover), "preferred": True}]}),
        encoding="utf-8",
    )

    def fake_call(cover_path, *, message_id, base_url, api_key, model, timeout=60):
        return {
            "message_id": message_id,
            "title": "Maybe",
            "episode": None,
            "is_drama": False,
            "confidence": 0.94,
            "reason": "",
            "cover_path": str(cover_path),
            "status": "model_ok",
        }

    monkeypatch.setattr("src.ocr_organizer.call_llm_vision_endpoint", fake_call)

    result = verify_cover_manifests(
        temp_dir / "covers",
        temp_dir / "covers",
        reject_non_drama=False,
        require_episode=False,
    )

    assert result["accepted"] == 1


def test_llm_verify_command_parser_defaults_are_conservative():
    args = build_parser().parse_args([
        "llm-verify",
        "--cover-root",
        "/covers",
        "--output-dir",
        "/covers",
    ])

    assert args.model == "gpt-5.5"
    assert args.min_confidence == 0.92
    assert args.require_episode is True
    assert args.reject_non_drama is True
