"""Emby/Jellyfin NFO generation for short-drama downloads."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from src.state import ShortDramaAssignment

if TYPE_CHECKING:
    from pyrogram.types import Message


def write_short_drama_nfo(
    video_path: Path,
    assignment: ShortDramaAssignment,
    message: "Message",
) -> tuple[Path | None, Path | None]:
    """Create series and episode NFO files for an episode assignment if missing."""
    if assignment.episode_number is None:
        return None, None

    series_nfo = write_tvshow_nfo(video_path.parent, assignment.series)
    episode_nfo = write_episode_nfo(video_path, assignment, message)
    return series_nfo, episode_nfo


def write_tvshow_nfo(series_dir: Path, series: str) -> Path | None:
    """Create tvshow.nfo for a series directory, preserving existing files."""
    path = series_dir / "tvshow.nfo"
    if path.exists():
        return None

    root = ElementTree.Element("tvshow")
    ElementTree.SubElement(root, "title").text = series
    _write_xml(path, root)
    return path


def write_episode_nfo(
    video_path: Path,
    assignment: ShortDramaAssignment,
    message: "Message",
) -> Path | None:
    """Create an episode NFO next to a video, preserving existing files."""
    path = video_path.with_suffix(".nfo")
    if path.exists():
        return None

    title = assignment.title or f"EP{assignment.episode_number:02d}"
    root = ElementTree.Element("episodedetails")
    ElementTree.SubElement(root, "title").text = title
    ElementTree.SubElement(root, "showtitle").text = assignment.series
    ElementTree.SubElement(root, "season").text = "1"
    ElementTree.SubElement(root, "episode").text = str(assignment.episode_number)
    ElementTree.SubElement(root, "plot").text = title

    aired = _format_air_date(getattr(message, "date", None))
    if aired:
        ElementTree.SubElement(root, "aired").text = aired

    uniqueid = ElementTree.SubElement(root, "uniqueid", {"type": "telegram_message"})
    uniqueid.text = str(message.id)

    _write_xml(path, root)
    return path


def write_organized_episode_nfo(
    video_path: Path,
    *,
    series: str,
    episode_number: int | None = None,
    title: str = "",
    message_id: int | None = None,
    aired: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Create Emby/Jellyfin NFO files for an organized hardlink item.

    This is used by the OCR organizer where we no longer have a live Pyrogram
    Message object, but the hardlink plan still carries series/title/episode and
    Telegram message_id metadata.
    """
    series_nfo = write_tvshow_nfo(video_path.parent, series)
    episode_nfo = video_path.with_suffix(".nfo")
    if episode_nfo.exists():
        return series_nfo, None

    display_title = title or (f"EP{episode_number:02d}" if episode_number else video_path.stem)
    root = ElementTree.Element("episodedetails")
    ElementTree.SubElement(root, "title").text = display_title
    ElementTree.SubElement(root, "showtitle").text = series
    ElementTree.SubElement(root, "season").text = "1"
    if episode_number is not None:
        ElementTree.SubElement(root, "episode").text = str(episode_number)
    ElementTree.SubElement(root, "plot").text = display_title
    if aired:
        ElementTree.SubElement(root, "aired").text = aired
    if message_id is not None:
        uniqueid = ElementTree.SubElement(root, "uniqueid", {"type": "telegram_message"})
        uniqueid.text = str(message_id)

    _write_xml(episode_nfo, root)
    return series_nfo, episode_nfo


def _format_air_date(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _write_xml(path: Path, root: ElementTree.Element) -> None:
    tree = ElementTree.ElementTree(root)
    ElementTree.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
