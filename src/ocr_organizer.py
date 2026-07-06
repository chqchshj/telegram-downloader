"""Optional OCR-map organizer for short-drama downloads.

This module does not perform OCR.  It extracts cover candidates for external
OCR tooling and can apply a reviewed OCR map into a separate hardlink view.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from src.media import is_short_drama_episode_media
from src.security.sanitizer import sanitize_filename, validate_path_safety


VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
COVER_SUFFIX = ".jpg"


@dataclass(frozen=True)
class HistoryRow:
    """Downloaded media row used by the organizer."""

    file_unique_id: str
    file_name: str
    file_size: int
    source_key: str
    message_id: int
    downloaded_at: str | None = None


@dataclass(frozen=True)
class PlanItem:
    """A planned or completed hardlink-view item."""

    message_id: int
    title: str
    episode: str
    source: str
    target: str
    cover: str | None = None
    action: str = "link"


def normalize_episode_suffix(episode: str | int | None) -> str:
    """Normalize episode input to ``EP01`` style suffixes."""
    if episode is None:
        return ""
    text = str(episode).strip()
    if not text:
        return ""
    match = re.search(r"\d+", text)
    if match:
        return f"EP{int(match.group(0)):02d}"
    return sanitize_filename(text).upper()


def normalize_title(title: str | None) -> str:
    """Normalize OCR title text for safe path components."""
    return sanitize_filename((title or "").strip())


def build_ocr_filename(
    title: str,
    episode: str | int | None,
    message_id: int,
    suffix: str,
) -> tuple[str, str]:
    """Return relative ``(folder, filename)`` components for an OCR item."""
    safe_title = normalize_title(title)
    episode_suffix = normalize_episode_suffix(episode)
    ext = suffix if suffix.startswith(".") else f".{suffix}"

    parts = [safe_title]
    if episode_suffix:
        parts.append(episode_suffix)
    parts.append(str(message_id))
    filename = sanitize_filename("_".join(parts) + ext)
    return safe_title, filename


def _candidate_paths(
    source_dir: Path,
    sanitized_name: str,
    original_suffix: str,
) -> Iterable[Path]:
    yield source_dir / sanitized_name
    stem = Path(sanitized_name).stem
    suffix = Path(sanitized_name).suffix or original_suffix
    for idx in range(2, 1000):
        yield source_dir / f"{stem}_{idx}{suffix}"


def resolve_downloaded_media_path(
    download_root: str | Path,
    source_folder: str | None,
    file_name: str,
    file_size: int,
) -> Path | None:
    """Resolve a history row to the downloaded file using downloader semantics."""
    root = Path(download_root)
    safe_name = sanitize_filename(file_name)
    original_suffix = Path(file_name).suffix

    source_dirs: list[Path]
    if source_folder:
        source_dirs = [root / sanitize_filename(source_folder)]
    else:
        source_dirs = [root]

    for source_dir in source_dirs:
        for candidate in _candidate_paths(source_dir, safe_name, original_suffix):
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size == file_size:
                return candidate
            if candidate.name == safe_name and not candidate.exists():
                break

    search_roots = source_dirs if source_folder else [root]
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for candidate in search_root.rglob(f"*{Path(safe_name).suffix}"):
            if candidate.is_file() and candidate.stat().st_size == file_size:
                return candidate
    return None


async def extract_cover_candidates(
    client: Any,
    chat_id: int | str,
    message_id: int,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Download media-group photo covers and video thumbnail fallback candidates."""
    destination = Path(output_dir) / str(message_id)
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "candidates": [],
    }

    group_messages = []
    try:
        group_messages = await client.get_media_group(chat_id, message_id)
    except Exception:
        group_messages = []

    photo_index = 0
    for group_msg in group_messages or []:
        if getattr(group_msg, "photo", None):
            photo_index += 1
            target = destination / f"group_photo_{photo_index}{COVER_SUFFIX}"
            downloaded = await client.download_media(group_msg, file_name=str(target))
            if downloaded:
                manifest["candidates"].append({
                    "kind": "group_photo",
                    "path": str(Path(downloaded)),
                    "preferred": True,
                })

    video_msg = await client.get_messages(chat_id=chat_id, message_ids=message_id)
    video = getattr(video_msg, "video", None) or getattr(video_msg, "document", None)
    thumbs = getattr(video, "thumbs", None) or []
    if thumbs:
        target = destination / f"video_thumb_1{COVER_SUFFIX}"
        downloaded = await client.download_media(thumbs[0], file_name=str(target))
        if downloaded:
            manifest["candidates"].append({
                "kind": "video_thumb",
                "path": str(Path(downloaded)),
                "preferred": not any(c["kind"] == "group_photo" for c in manifest["candidates"]),
            })

    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _load_history_rows(state_db: str | Path) -> list[HistoryRow]:
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT file_unique_id, file_name, file_size, source_key, message_id, downloaded_at
            FROM download_history
            ORDER BY message_id
            """
        ).fetchall()
    finally:
        connection.close()
    return [HistoryRow(**dict(row)) for row in rows]


def _coerce_history_rows(rows: Iterable[HistoryRow | dict[str, Any]]) -> list[HistoryRow]:
    coerced = []
    for row in rows:
        if isinstance(row, HistoryRow):
            coerced.append(row)
        else:
            coerced.append(HistoryRow(**row))
    return coerced


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    path = Path(path)
    if path.is_dir():
        manifest: dict[str, Any] = {}
        for manifest_path in path.glob("*/manifest.json"):
            data = _load_json(manifest_path)
            message_id = data.get("message_id")
            if message_id is not None:
                manifest[str(message_id)] = data
        return manifest
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _ocr_entry(ocr_map: dict[str, Any], message_id: int) -> dict[str, Any] | None:
    value = ocr_map.get(str(message_id), ocr_map.get(message_id))
    return value if isinstance(value, dict) else None


def _cover_for_message(cover_manifest: dict[str, Any], message_id: int) -> Path | None:
    direct = cover_manifest.get(str(message_id), cover_manifest.get(message_id))
    if isinstance(direct, str):
        return Path(direct)
    if isinstance(direct, dict):
        candidates = direct.get("candidates", [])
    else:
        candidates = cover_manifest.get("candidates", []) if cover_manifest.get("message_id") == message_id else []
    for candidate in candidates:
        if candidate.get("preferred") and candidate.get("path"):
            return Path(candidate["path"])
    for candidate in candidates:
        if candidate.get("path"):
            return Path(candidate["path"])
    return None


def _unique_target(path: Path, used_targets: set[Path] | None = None) -> Path:
    used_targets = used_targets or set()
    if not path.exists() and path not in used_targets:
        return path
    stem = path.stem
    suffix = path.suffix
    for idx in range(2, 1000):
        candidate = path.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists() and candidate not in used_targets:
            return candidate
    raise RuntimeError(f"Cannot find available target name for {path}")


def _copy_or_link_cover(source: Path, target: Path, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def create_hardlink_view(
    download_history_rows: Iterable[HistoryRow | dict[str, Any]],
    ocr_map: dict[str, Any] | str | Path,
    cover_manifest: dict[str, Any] | str | Path | None,
    download_root: str | Path,
    output_root: str | Path,
    source_folder: str | None = None,
    min_confidence: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a separate hardlink folder from history rows and an OCR map."""
    rows = _coerce_history_rows(download_history_rows)
    ocr_data = _load_json(ocr_map) if isinstance(ocr_map, (str, Path)) else ocr_map
    cover_data = _load_json(cover_manifest) if isinstance(cover_manifest, (str, Path)) else (cover_manifest or {})
    output = Path(output_root)
    download = Path(download_root)
    if output.resolve() == download.resolve() or output.resolve().is_relative_to(download.resolve()):
        raise ValueError("output_root must be separate from download_root")

    plan: list[PlanItem] = []
    skipped: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    used_targets: set[Path] = set()

    for row in rows:
        entry = _ocr_entry(ocr_data, row.message_id)
        if not entry:
            skipped.append({"message_id": row.message_id, "reason": "no_ocr_entry"})
            continue
        title = str(entry.get("title") or "").strip()
        confidence = float(entry.get("confidence") or 0)
        if not title:
            skipped.append({"message_id": row.message_id, "reason": "no_title"})
            continue
        if confidence < min_confidence:
            skipped.append({
                "message_id": row.message_id,
                "reason": "low_confidence",
                "confidence": confidence,
            })
            continue

        source = resolve_downloaded_media_path(
            download,
            source_folder,
            row.file_name,
            row.file_size,
        )
        if source is None:
            missing.append({"message_id": row.message_id, "file_name": row.file_name})
            continue
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            skipped.append({"message_id": row.message_id, "reason": "not_video"})
            continue

        folder, filename = build_ocr_filename(
            title,
            entry.get("episode"),
            row.message_id,
            source.suffix,
        )
        target = validate_path_safety(output, output / folder / filename)
        target = _unique_target(target, used_targets)
        used_targets.add(target)

        cover_path = _cover_for_message(cover_data, row.message_id)
        cover_target = None
        if cover_path and cover_path.exists():
            cover_target = target.with_suffix(cover_path.suffix or COVER_SUFFIX)

        plan.append(PlanItem(
            message_id=row.message_id,
            title=title,
            episode=normalize_episode_suffix(entry.get("episode")),
            source=str(source),
            target=str(target),
            cover=str(cover_target) if cover_target else None,
            action="dry-run" if dry_run else "link",
        ))

    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "_hardlink_plan.json"
    skipped_path = output / "_skipped.json"
    missing_path = output / "_missing.json"

    if not dry_run:
        for item in plan:
            source = Path(item.source)
            target = Path(item.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.link(source, target)
            if item.cover:
                cover_source = _cover_for_message(cover_data, item.message_id)
                if cover_source and cover_source.exists():
                    _copy_or_link_cover(cover_source, Path(item.cover), dry_run=False)

    plan_path.write_text(json.dumps([asdict(item) for item in plan], ensure_ascii=False, indent=2), encoding="utf-8")
    skipped_path.write_text(json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "planned": len(plan),
        "skipped": len(skipped),
        "missing": len(missing),
        "plan_path": str(plan_path),
        "skipped_path": str(skipped_path),
        "missing_path": str(missing_path),
    }


async def run_extract_covers(args: argparse.Namespace) -> None:
    from src.client import create_client
    from src.config import load_config
    from src.config.source_parser import parse_sources

    cfg = load_config(args.config)
    client = create_client(cfg)
    async with client:
        sources_with_filters = await parse_sources(cfg, client)
        if args.source_index is not None:
            try:
                source = sources_with_filters[args.source_index][0]
            except IndexError:
                raise SystemExit(f"source index out of range: {args.source_index}")
        else:
            source = None
            for idx, source_config in enumerate(cfg.sources):
                if source_config.url == args.source_url:
                    source = sources_with_filters[idx][0]
                    break
            if source is None:
                raise SystemExit("source not found; use --source-index or matching --source-url")

        async for msg in source.iterate_new_media(args.min_message_id - 1):
            media = getattr(msg, "video", None) or getattr(msg, "document", None)
            if is_short_drama_episode_media(msg, media):
                await extract_cover_candidates(client, source.chat_id, msg.id, args.output_dir)


def run_apply_map(args: argparse.Namespace) -> None:
    rows = _load_history_rows(args.state_db)
    result = create_hardlink_view(
        rows,
        args.ocr_map,
        args.cover_root,
        args.download_root,
        args.output_root,
        source_folder=args.source_folder,
        min_confidence=args.min_confidence,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCR-map organizer for Telegram downloads")
    subcommands = parser.add_subparsers(dest="command", required=True)

    extract = subcommands.add_parser("extract-covers")
    extract.add_argument("--config", required=True)
    source_group = extract.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-url")
    source_group.add_argument("--source-index", type=int)
    extract.add_argument("--output-dir", required=True)
    extract.add_argument("--min-message-id", type=int, required=True)
    extract.set_defaults(func=lambda args: asyncio.run(run_extract_covers(args)))

    apply = subcommands.add_parser("apply-map")
    apply.add_argument("--download-root", required=True)
    apply.add_argument("--state-db", required=True)
    apply.add_argument("--ocr-map", required=True)
    apply.add_argument("--cover-root")
    apply.add_argument("--output-root", required=True)
    apply.add_argument("--source-folder")
    apply.add_argument("--min-confidence", type=float, default=0.0)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=run_apply_map)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
