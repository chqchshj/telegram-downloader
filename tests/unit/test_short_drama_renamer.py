"""Tests for the offline short-drama renamer."""
import os
import sqlite3

from src.tools.short_drama_renamer import build_rename_plan


CATALOG = """美丽新世界 EP-1 樱花道偶遇
美丽新世界 EP-2 误入厕所成变态
"""


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
    (temp_dir / "old.mp4").write_text("old")
    (temp_dir / "美丽新世界_EP01_樱花道偶遇.mp4").write_text("existing")

    plan = build_rename_plan(temp_dir, "美丽新世界 EP-1 樱花道偶遇")

    assert plan[0].source.name == "old.mp4"
    assert plan[0].target.name == "美丽新世界_EP01_樱花道偶遇_2.mp4"


def test_build_rename_plan_uses_state_db_message_order(temp_dir):
    db_path = temp_dir / "state.db"
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
    connection.execute(
        "INSERT INTO download_history VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        ("uid-2", "second.mp4", 1, "source", 20),
    )
    connection.execute(
        "INSERT INTO download_history VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        ("uid-1", "first.mp4", 1, "source", 10),
    )
    connection.commit()
    connection.close()
    (temp_dir / "second.mp4").write_text("2")
    (temp_dir / "first.mp4").write_text("1")

    plan = build_rename_plan(temp_dir, CATALOG, state_db=db_path)

    assert [item.source.name for item in plan] == ["first.mp4", "second.mp4"]
