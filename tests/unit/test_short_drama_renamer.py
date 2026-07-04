"""Tests for the offline short-drama renamer."""
import os
import sqlite3

from src.tools.short_drama_renamer import build_rename_plan


CATALOG = """美丽新世界 EP-1 樱花道偶遇
美丽新世界 EP-2 误入厕所成变态
"""


def _create_history_db(db_path, rows):
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE download_history (
            file_unique_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for unique_id, file_name, file_size, message_id in rows:
        connection.execute(
            "INSERT INTO download_history VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (unique_id, file_name, file_size, "source", message_id),
        )
    connection.commit()
    connection.close()


def test_build_rename_plan_dry_run_orders_by_mtime(temp_dir):
    second = temp_dir / "download-b.mp4"
    first = temp_dir / "download-a.mp4"
    second.write_text("2")
    first.write_text("1")
    os.utime(first, (100, 100))
    os.utime(second, (200, 200))

    plan = build_rename_plan(temp_dir, CATALOG)

    assert [item.source.name for item in plan] == ["download-a.mp4", "download-b.mp4"]
    assert [item.target.name for item in plan] == [
        "美丽新世界_EP01_樱花道偶遇.mp4",
        "美丽新世界_EP02_误入厕所成变态.mp4",
    ]
    assert first.exists()
    assert second.exists()


def test_build_rename_plan_adds_suffix_when_target_exists(temp_dir):
    series_dir = temp_dir / "美丽新世界"
    series_dir.mkdir()
    (temp_dir / "old.mp4").write_text("old")
    (series_dir / "美丽新世界_EP01_樱花道偶遇.mp4").write_text("existing")

    plan = build_rename_plan(temp_dir, "美丽新世界 EP-1 樱花道偶遇")

    assert plan[0].source.name == "old.mp4"
    assert plan[0].target.name == "美丽新世界_EP01_樱花道偶遇_2.mp4"


def test_build_rename_plan_uses_state_db_message_order(temp_dir):
    db_path = temp_dir / "state.db"
    _create_history_db(
        db_path,
        [("uid-2", "second.mp4", 1, 20), ("uid-1", "first.mp4", 1, 10)],
    )
    (temp_dir / "second.mp4").write_text("2")
    (temp_dir / "first.mp4").write_text("1")

    plan = build_rename_plan(temp_dir, CATALOG, state_db=db_path)

    assert [item.source.name for item in plan] == ["first.mp4", "second.mp4"]


def test_build_rename_plan_targets_existing_single_series_subdir(temp_dir):
    series_dir = temp_dir / "_____"
    series_dir.mkdir()
    source = series_dir / "old.mp4"
    source.write_text("old")

    plan = build_rename_plan(temp_dir, "美丽新世界 EP-1 樱花道偶遇")

    assert plan[0].source == source
    assert plan[0].target.parent == series_dir
    assert plan[0].target.name == "美丽新世界_EP01_樱花道偶遇.mp4"


def test_build_rename_plan_filters_state_db_by_message_id(temp_dir):
    db_path = temp_dir / "state.db"
    _create_history_db(
        db_path,
        [
            ("old", "unrelated.mp4", 1, 55),
            ("ep1", "first.mp4", 1, 1619),
            ("ep2", "second.mp4", 1, 1621),
        ],
    )
    for name in ["unrelated.mp4", "first.mp4", "second.mp4"]:
        (temp_dir / name).write_text(name)

    plan = build_rename_plan(temp_dir, CATALOG, state_db=db_path, min_message_id=1619)

    assert [item.source.name for item in plan] == ["first.mp4", "second.mp4"]


def test_build_rename_plan_matches_sanitized_files_by_state_size(temp_dir):
    db_path = temp_dir / "state.db"
    _create_history_db(
        db_path,
        [
            ("ep1", "美丽新世界 EP-1 樱花道偶遇.mp4", 11, 1619),
            ("ep2", "美丽新世界 EP-2 误入厕所成变态.mp4", 22, 1621),
        ],
    )
    first = temp_dir / "_____" / "______1.mp4"
    second = temp_dir / "_____" / "______2.mp4"
    first.parent.mkdir()
    first.write_bytes(b"1" * 11)
    second.write_bytes(b"2" * 22)

    plan = build_rename_plan(temp_dir, CATALOG, state_db=db_path, min_message_id=1619)

    assert [item.source.name for item in plan] == ["______1.mp4", "______2.mp4"]
    assert [item.target.name for item in plan] == [
        "美丽新世界_EP01_樱花道偶遇.mp4",
        "美丽新世界_EP02_误入厕所成变态.mp4",
    ]
