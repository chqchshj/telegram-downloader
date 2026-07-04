"""Offline short-drama episode renaming tool."""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from src.media import CaptionEpisode, build_episode_filename, parse_caption_episodes


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
    video_extensions: Sequence[str] = DEFAULT_VIDEO_EXTENSIONS,
) -> list[RenamePlanItem]:
    """Build a collision-safe rename plan for downloaded short-drama videos."""
    episodes = parse_caption_episodes(catalog_caption)
    video_files = _ordered_video_files(downloads_dir, state_db, video_extensions)
    occupied = {path.resolve() for path in downloads_dir.rglob("*") if path.is_file()}

    plan: list[RenamePlanItem] = []
    for source, episode in zip(video_files, episodes):
        desired = downloads_dir / build_episode_filename(episode, source.suffix)
        target = _unique_target_path(desired, source, occupied)
        occupied.add(target.resolve())
        plan.append(RenamePlanItem(source=source, target=target))

    return plan


def apply_rename_plan(plan: Iterable[RenamePlanItem]) -> None:
    """Apply a previously generated rename plan."""
    for item in plan:
        if item.is_noop:
            continue
        item.source.rename(item.target)


def _ordered_video_files(
    downloads_dir: Path,
    state_db: Path | None,
    video_extensions: Sequence[str],
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
    for file_name in _state_file_names_by_message_id(state_db):
        candidates = []
        recorded_path = (downloads_dir / file_name).resolve()
        if recorded_path in by_resolved:
            candidates.append(by_resolved[recorded_path])
        candidates.extend(by_name.get(Path(file_name).name, []))

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                ordered.append(candidate)
                seen.add(resolved)
                break

    remaining = [path for path in files if path.resolve() not in seen]
    remaining.sort(key=lambda path: (path.stat().st_mtime, path.name))
    return ordered + remaining


def _state_file_names_by_message_id(state_db: Path | None) -> list[str]:
    """Return recorded history filenames ordered by Telegram message_id."""
    if not state_db or not state_db.exists():
        return []

    try:
        with sqlite3.connect(state_db) as connection:
            rows = connection.execute(
                """
                SELECT file_name
                FROM download_history
                ORDER BY message_id ASC, file_name ASC
                """
            ).fetchall()
    except sqlite3.Error:
        return []

    return [row[0] for row in rows]


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
        video_extensions=_parse_extensions(args.video_extension),
    )

    for item in plan:
        print(f"{item.source.name} -> {item.target.name}")

    if args.apply:
        apply_rename_plan(plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
