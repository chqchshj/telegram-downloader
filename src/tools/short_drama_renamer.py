"""Offline short-drama episode renaming tool."""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from src.media import CaptionEpisode, build_episode_filename, parse_caption_episodes
from src.security.sanitizer import sanitize_filename


@dataclass(frozen=True)
class DownloadRecord:
    """A downloaded file recorded in state.db history."""

    file_name: str
    file_size: int


DEFAULT_VIDEO_EXTENSIONS = (".mp4",)


@dataclass(frozen=True)
class RenamePlanItem:
    """One planned filesystem rename."""

    source: Path
    target: Path

    @property
    def is_noop(self) -> bool:
        return self.source == self.target


def build_rename_plan(
    downloads_dir: Path,
    catalog_caption: str,
    *,
    state_db: Path | None = None,
    min_message_id: int | None = None,
    max_message_id: int | None = None,
    video_extensions: Sequence[str] = DEFAULT_VIDEO_EXTENSIONS,
) -> list[RenamePlanItem]:
    """Build a collision-safe rename plan for downloaded short-drama videos."""
    episodes = parse_caption_episodes(catalog_caption)
    video_files = _ordered_video_files(
        downloads_dir,
        state_db,
        video_extensions,
        min_message_id=min_message_id,
        max_message_id=max_message_id,
    )
    occupied = {path.resolve() for path in downloads_dir.rglob("*") if path.is_file()}

    plan: list[RenamePlanItem] = []
    output_dir = _series_output_dir(downloads_dir, episodes)
    for source, episode in zip(video_files, episodes):
        desired = output_dir / build_episode_filename(episode, source.suffix)
        target = _unique_target_path(desired, source, occupied)
        occupied.add(target.resolve())
        plan.append(RenamePlanItem(source=source, target=target))

    return plan


def apply_rename_plan(plan: Iterable[RenamePlanItem]) -> None:
    """Apply a previously generated rename plan."""
    for item in plan:
        if item.is_noop:
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.source.rename(item.target)


def _series_output_dir(downloads_dir: Path, episodes: Sequence[CaptionEpisode]) -> Path:
    """Return the existing or preferred subdirectory for a short-drama series."""
    if not episodes:
        return downloads_dir

    safe_series = sanitize_filename(episodes[0].series)
    direct = downloads_dir / safe_series
    if direct.is_dir():
        return direct

    # Existing NAS downloads may already live under a sanitized/garbled source
    # folder such as "_____". Prefer the only child directory when present so a
    # dry-run previews in-place renames instead of a surprise move.
    child_dirs = [p for p in downloads_dir.iterdir() if p.is_dir()]
    if len(child_dirs) == 1:
        return child_dirs[0]
    return direct


def _ordered_video_files(
    downloads_dir: Path,
    state_db: Path | None,
    video_extensions: Sequence[str],
    *,
    min_message_id: int | None = None,
    max_message_id: int | None = None,
) -> list[Path]:
    """Order video files by state message_id when available, else mtime/name."""
    extensions = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in video_extensions}
    files = [
        path
        for path in downloads_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    ]
    by_resolved = {path.resolve(): path for path in files}
    by_name: dict[str, list[Path]] = {}
    for path in files:
        by_name.setdefault(path.name, []).append(path)

    ordered: list[Path] = []
    seen: set[Path] = set()
    records = _state_records_by_message_id(
        state_db,
        min_message_id=min_message_id,
        max_message_id=max_message_id,
    )
    video_records = [
        record
        for record in records
        if Path(record.file_name).suffix.lower() in extensions
    ]
    if video_records and _looks_like_sanitized_download_tree(files, video_records):
        # Existing files were produced through sanitize_filename(), while
        # download_history stores original Telegram names. Reconstruct the same
        # ordering by size when names no longer match (common for Chinese names).
        ordered_by_size = _match_records_by_size(files, video_records)
        if ordered_by_size:
            return ordered_by_size

    for record in video_records:
        record_name = Path(record.file_name).name

        candidates = []
        recorded_path = (downloads_dir / record.file_name).resolve()
        if recorded_path in by_resolved:
            candidates.append(by_resolved[recorded_path])
        candidates.extend(by_name.get(record_name, []))
        candidates.extend(by_name.get(sanitize_filename(record_name), []))

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                ordered.append(candidate)
                seen.add(resolved)
                break

    # When a caller provides explicit message-id bounds, the state database is
    # the source of truth for the desired slice. Do not append unrelated older
    # files by mtime, or a catalog starting at message 1619 could accidentally
    # rename downloads from message 51.
    if (min_message_id is not None or max_message_id is not None) and ordered:
        return ordered

    remaining = [path for path in files if path.resolve() not in seen]
    remaining.sort(key=lambda path: (path.stat().st_mtime, path.name))
    return ordered + remaining


def _state_records_by_message_id(
    state_db: Path | None,
    *,
    min_message_id: int | None = None,
    max_message_id: int | None = None,
) -> list[DownloadRecord]:
    """Return recorded history rows ordered by Telegram message_id."""
    if not state_db or not state_db.exists():
        return []

    try:
        with sqlite3.connect(state_db) as connection:
            where: list[str] = []
            params: list[int] = []
            if min_message_id is not None:
                where.append("message_id >= ?")
                params.append(min_message_id)
            if max_message_id is not None:
                where.append("message_id <= ?")
                params.append(max_message_id)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            rows = connection.execute(
                f"""
                SELECT file_name, file_size
                FROM download_history
                {where_sql}
                ORDER BY message_id ASC, file_name ASC
                """,
                params,
            ).fetchall()
    except sqlite3.Error:
        return []

    return [DownloadRecord(file_name=row[0], file_size=int(row[1] or 0)) for row in rows]


def _looks_like_sanitized_download_tree(files: Sequence[Path], records: Sequence[DownloadRecord]) -> bool:
    """Detect when state names do not match filesystem names but sizes can."""
    file_names = {path.name for path in files}
    record_names = {record.file_name for record in records}
    return bool(files and records and not file_names.intersection(record_names))


def _match_records_by_size(files: Sequence[Path], records: Sequence[DownloadRecord]) -> list[Path]:
    """Match state records to files by unique file size in message order."""
    by_size: dict[int, list[Path]] = {}
    for path in files:
        by_size.setdefault(path.stat().st_size, []).append(path)

    ordered: list[Path] = []
    seen: set[Path] = set()
    for record in records:
        candidates = by_size.get(record.file_size, [])
        available = [path for path in candidates if path.resolve() not in seen]
        if len(available) != 1:
            return []
        chosen = available[0]
        ordered.append(chosen)
        seen.add(chosen.resolve())
    return ordered


def _unique_target_path(desired: Path, source: Path, occupied: set[Path]) -> Path:
    """Return a target path that will not overwrite existing files."""
    desired_resolved = desired.resolve()
    source_resolved = source.resolve()
    if desired_resolved == source_resolved or desired_resolved not in occupied:
        return desired

    counter = 2
    while True:
        candidate = desired.with_name(f"{desired.stem}_{counter}{desired.suffix}")
        candidate_resolved = candidate.resolve()
        if candidate_resolved == source_resolved or candidate_resolved not in occupied:
            return candidate
        counter += 1


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_extensions(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return DEFAULT_VIDEO_EXTENSIONS

    extensions: list[str] = []
    for value in values:
        for raw_ext in value.split(","):
            ext = raw_ext.strip().lower()
            if not ext:
                continue
            extensions.append(ext if ext.startswith(".") else f".{ext}")
    return tuple(extensions) or DEFAULT_VIDEO_EXTENSIONS


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply offline short-drama video renames from a catalog caption.",
    )
    parser.add_argument("--downloads-dir", required=True, type=Path)
    parser.add_argument("--catalog-caption-file", required=True, type=Path)
    parser.add_argument("--state-db", type=Path, help="Optional downloader state.db for message_id ordering.")
    parser.add_argument("--min-message-id", type=int, help="Only use state.db history at or after this Telegram message ID.")
    parser.add_argument("--max-message-id", type=int, help="Only use state.db history at or before this Telegram message ID.")
    parser.add_argument(
        "--video-extension",
        action="append",
        help="Video extension to process. Defaults to .mp4. Can be repeated or comma-separated.",
    )
    parser.add_argument("--apply", action="store_true", help="Rename files. Without this flag, only print the plan.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    downloads_dir = args.downloads_dir
    if not downloads_dir.is_dir():
        raise SystemExit(f"downloads dir does not exist: {downloads_dir}")

    caption = _read_text(args.catalog_caption_file)
    plan = build_rename_plan(
        downloads_dir,
        caption,
        state_db=args.state_db,
        min_message_id=args.min_message_id,
        max_message_id=args.max_message_id,
        video_extensions=_parse_extensions(args.video_extension),
    )

    for item in plan:
        print(f"{item.source.name} -> {item.target.name}")

    if args.apply:
        apply_rename_plan(plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
