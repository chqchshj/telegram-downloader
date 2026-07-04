"""SQLite state for runtime short-drama catalog naming."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from src.media import CaptionEpisode, build_episode_filename, episode_number

from .cursor import StateError


@dataclass(frozen=True)
class ShortDramaCatalog:
    """Active catalog state for one source."""

    cursor_key: str
    series: str
    episodes: list[CaptionEpisode]
    next_index: int
    catalog_message_id: int


@dataclass(frozen=True)
class ShortDramaAssignment:
    """Runtime filename/folder assignment for one Telegram message."""

    series: str
    filename: str
    folder_name: str
    catalog_message_id: int
    episode_index: int | None = None
    episode_number: int | None = None
    title: str = ""


class ShortDramaState:
    """Persistent short-drama catalog state scoped by source cursor key."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path)
        self._connection.execute("PRAGMA journal_mode=WAL")

        result = self._connection.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            raise StateError(f"Database integrity check failed: {result[0]}")

        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS short_drama_catalog_state (
                cursor_key TEXT PRIMARY KEY,
                series TEXT NOT NULL,
                episodes_json TEXT NOT NULL,
                next_index INTEGER NOT NULL,
                catalog_message_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS short_drama_episode_assignments (
                cursor_key TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                series TEXT NOT NULL,
                filename TEXT NOT NULL,
                folder_name TEXT NOT NULL,
                catalog_message_id INTEGER NOT NULL,
                episode_index INTEGER,
                episode_number INTEGER,
                title TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (cursor_key, message_id)
            )
        """)
        self._ensure_column(
            "short_drama_episode_assignments",
            "episode_number",
            "INTEGER",
        )
        self._ensure_column(
            "short_drama_episode_assignments",
            "title",
            "TEXT DEFAULT ''",
        )
        self._connection.commit()

    def _check_connection(self) -> None:
        if not self._connection:
            raise StateError("Database connection is closed")

    def set_catalog(
        self,
        cursor_key: str,
        catalog_message_id: int,
        episodes: list[CaptionEpisode],
    ) -> ShortDramaCatalog:
        """Replace the active catalog for a source and reset episode consumption."""
        self._check_connection()
        if not episodes:
            raise StateError("Cannot set short-drama catalog without episodes")

        series = episodes[0].series
        payload = json.dumps([asdict(episode) for episode in episodes], ensure_ascii=False)
        self._execute_with_retry(
            """
            INSERT INTO short_drama_catalog_state
                (cursor_key, series, episodes_json, next_index, catalog_message_id, updated_at)
            VALUES (?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cursor_key) DO UPDATE SET
                series = excluded.series,
                episodes_json = excluded.episodes_json,
                next_index = CASE
                    WHEN short_drama_catalog_state.series = excluded.series
                     AND short_drama_catalog_state.episodes_json = excluded.episodes_json
                    THEN short_drama_catalog_state.next_index
                    ELSE 0
                END,
                catalog_message_id = excluded.catalog_message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (cursor_key, series, payload, catalog_message_id),
        )
        return ShortDramaCatalog(cursor_key, series, episodes, 0, catalog_message_id)

    def get_catalog(self, cursor_key: str) -> ShortDramaCatalog | None:
        """Return the active catalog for a source, if one exists."""
        self._check_connection()
        cursor = self._connection.execute(
            """
            SELECT series, episodes_json, next_index, catalog_message_id
            FROM short_drama_catalog_state
            WHERE cursor_key = ?
            """,
            (cursor_key,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        series, episodes_json, next_index, catalog_message_id = row
        episodes = [
            CaptionEpisode(**episode)
            for episode in json.loads(episodes_json)
        ]
        return ShortDramaCatalog(
            cursor_key=cursor_key,
            series=series,
            episodes=episodes,
            next_index=next_index,
            catalog_message_id=catalog_message_id,
        )

    def get_assignment(
        self,
        cursor_key: str,
        message_id: int,
    ) -> ShortDramaAssignment | None:
        """Return a previous per-message assignment, if present."""
        self._check_connection()
        cursor = self._connection.execute(
            """
            SELECT series, filename, folder_name, catalog_message_id,
                   episode_index, episode_number, title
            FROM short_drama_episode_assignments
            WHERE cursor_key = ? AND message_id = ?
            """,
            (cursor_key, message_id),
        )
        row = cursor.fetchone()
        if not row:
            return None

        return ShortDramaAssignment(
            series=row[0],
            filename=row[1],
            folder_name=row[2],
            catalog_message_id=row[3],
            episode_index=row[4],
            episode_number=row[5],
            title=row[6] or "",
        )

    def assign_catalog_media(
        self,
        cursor_key: str,
        message_id: int,
        filename: str,
        series: str,
        catalog_message_id: int,
    ) -> ShortDramaAssignment:
        """Persist the name for a catalog media message without consuming an episode."""
        existing = self.get_assignment(cursor_key, message_id)
        if existing:
            return existing

        assignment = ShortDramaAssignment(
            series=series,
            filename=filename,
            folder_name=series,
            catalog_message_id=catalog_message_id,
            episode_index=None,
            episode_number=None,
            title="",
        )
        self._insert_assignment(cursor_key, message_id, assignment)
        return assignment

    def assign_episode(
        self,
        cursor_key: str,
        message_id: int,
        episode: CaptionEpisode,
        ext: str,
        catalog_message_id: int | None = None,
        episode_index: int | None = None,
    ) -> ShortDramaAssignment:
        """Persist an explicit episode assignment without consuming catalog state."""
        existing = self.get_assignment(cursor_key, message_id)
        if existing:
            return existing

        assignment = ShortDramaAssignment(
            series=episode.series,
            filename=build_episode_filename(episode, ext),
            folder_name=episode.series,
            catalog_message_id=catalog_message_id or message_id,
            episode_index=episode_index,
            episode_number=episode_number(episode),
            title=episode.title,
        )
        self._insert_assignment(cursor_key, message_id, assignment)
        return assignment

    def assign_next_episode(
        self,
        cursor_key: str,
        message_id: int,
        ext: str,
    ) -> ShortDramaAssignment | None:
        """Assign the next catalog episode to a message and advance state once."""
        self._check_connection()

        existing = self.get_assignment(cursor_key, message_id)
        if existing:
            return existing

        max_retries = 3
        delays = [0.1, 0.2, 0.4]

        for attempt in range(max_retries):
            try:
                self._connection.execute("BEGIN")
                row = self._connection.execute(
                    """
                    SELECT series, episodes_json, next_index, catalog_message_id
                    FROM short_drama_catalog_state
                    WHERE cursor_key = ?
                    """,
                    (cursor_key,),
                ).fetchone()
                if not row:
                    self._connection.rollback()
                    return None

                series, episodes_json, next_index, catalog_message_id = row
                episodes = [
                    CaptionEpisode(**episode)
                    for episode in json.loads(episodes_json)
                ]
                if next_index >= len(episodes):
                    self._connection.rollback()
                    return None

                episode = episodes[next_index]
                assignment = ShortDramaAssignment(
                    series=series,
                    filename=build_episode_filename(episode, ext),
                    folder_name=series,
                    catalog_message_id=catalog_message_id,
                    episode_index=next_index,
                    episode_number=episode_number(episode),
                    title=episode.title,
                )
                self._connection.execute(
                    """
                    INSERT INTO short_drama_episode_assignments
                        (cursor_key, message_id, series, filename, folder_name,
                         catalog_message_id, episode_index, episode_number, title, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        cursor_key,
                        message_id,
                        assignment.series,
                        assignment.filename,
                        assignment.folder_name,
                        assignment.catalog_message_id,
                        assignment.episode_index,
                        assignment.episode_number,
                        assignment.title,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE short_drama_catalog_state
                    SET next_index = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE cursor_key = ?
                    """,
                    (next_index + 1, cursor_key),
                )
                self._connection.commit()
                return assignment
            except sqlite3.IntegrityError:
                self._connection.rollback()
                return self.get_assignment(cursor_key, message_id)
            except sqlite3.OperationalError as e:
                self._connection.rollback()
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(delays[attempt])
                    continue
                raise StateError(f"Database operation failed: {e}")
            except Exception as e:
                self._connection.rollback()
                raise StateError(f"Unexpected error during short-drama assignment: {e}")

        raise StateError(f"Failed to assign episode after {max_retries} retries (database locked)")

    def _insert_assignment(
        self,
        cursor_key: str,
        message_id: int,
        assignment: ShortDramaAssignment,
    ) -> None:
        self._execute_with_retry(
            """
            INSERT OR IGNORE INTO short_drama_episode_assignments
                (cursor_key, message_id, series, filename, folder_name,
                 catalog_message_id, episode_index, episode_number, title, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cursor_key,
                message_id,
                assignment.series,
                assignment.filename,
                assignment.folder_name,
                assignment.catalog_message_id,
                assignment.episode_index,
                assignment.episode_number,
                assignment.title,
            ),
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row[1]
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def _execute_with_retry(self, statement: str, parameters: tuple[object, ...]) -> None:
        max_retries = 3
        delays = [0.1, 0.2, 0.4]

        for attempt in range(max_retries):
            try:
                self._connection.execute("BEGIN")
                self._connection.execute(statement, parameters)
                self._connection.commit()
                return
            except sqlite3.OperationalError as e:
                self._connection.rollback()
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(delays[attempt])
                    continue
                raise StateError(f"Database operation failed: {e}")
            except Exception as e:
                self._connection.rollback()
                raise StateError(f"Unexpected short-drama state error: {e}")

        raise StateError(f"Database operation failed after {max_retries} retries (database locked)")

    def close(self) -> None:
        """Close database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
