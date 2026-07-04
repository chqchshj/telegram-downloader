"""Helpers for Telegram media detection and fallback naming."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.security.sanitizer import sanitize_filename

if TYPE_CHECKING:
    from pyrogram.types import Message


@dataclass(frozen=True)
class CaptionEpisode:
    """Series/episode data parsed from a Telegram caption."""
    series: str
    episode: str
    title: str = ""


MEDIA_FIELDS = (
    "document",
    "audio",
    "video",
    "animation",
    "voice",
    "video_note",
    "photo",
)


def get_message_media(message: Message) -> Any | None:
    """Return the first supported downloadable media object on a message."""
    for field in MEDIA_FIELDS:
        media = getattr(message, field, None)
        if media:
            return media
    return None


def get_caption_text(message: Message) -> str:
    """Return caption/text usable for fallback filenames and pattern matching."""
    return (
        getattr(message, "caption", None)
        or getattr(message, "text", None)
        or ""
    ).strip()


def parse_caption_episode(caption: str) -> CaptionEpisode | None:
    """Parse captions like ``美丽新世界 EP-1 樱花道偶遇``."""
    episodes = parse_caption_episodes(caption)
    return episodes[0] if episodes else None


def parse_caption_episodes(caption: str) -> list[CaptionEpisode]:
    """Parse all short-drama episode lines from a Telegram caption."""
    episodes: list[CaptionEpisode] = []

    seen_keys: set[tuple[str, str, str]] = set()
    for raw_line in caption.splitlines():
        line = re.sub(r"^[^\w]+", "", raw_line.strip())
        if not line:
            continue

        episode = _parse_episode_line(line)
        if episode:
            key = (episode.series, episode.episode, episode.title)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            episodes.append(episode)

    return episodes


def parse_catalog_episodes(caption: str) -> list[CaptionEpisode]:
    """Return catalog episodes from caption/text, including single-episode posts."""
    return parse_caption_episodes(caption)


def get_message_catalog_episodes(message: Message) -> list[CaptionEpisode]:
    """Return parsed catalog episodes from a Telegram message caption/text."""
    return parse_catalog_episodes(get_caption_text(message))


def is_catalog_message(message: Message) -> bool:
    """Return True when a message caption/text is a short-drama catalog."""
    return bool(get_message_catalog_episodes(message))


def _parse_episode_line(line: str) -> CaptionEpisode | None:
    """Parse one caption line into series, normalized EP marker, and title."""
    patterns = [
        r"^\s*(?P<series>.+?)\s+(?P<episode>EP\s*[-_ ]?\s*\d+)\s*(?P<title>.*)$",
        r"^\s*(?P<series>.+?)\s+(?P<episode>[第全]\s*\d+\s*集)\s*(?P<title>.*)$",
    ]
    match = None
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            break

    if not match:
        return None

    episode = _normalize_episode_marker(match.group("episode"))

    return CaptionEpisode(
        series=match.group("series").strip(),
        episode=episode,
        title=match.group("title").strip(),
    )


def _normalize_episode_marker(marker: str) -> str:
    """Normalize supported episode markers to EP-N."""
    number_match = re.search(r"\d+", marker)
    number = int(number_match.group(0)) if number_match else 0
    return f"EP-{number}"


def episode_number(episode: CaptionEpisode) -> int | None:
    """Return an integer episode number from a parsed episode marker."""
    number_match = re.search(r"\d+", episode.episode)
    return int(number_match.group(0)) if number_match else None


def build_episode_filename(episode: CaptionEpisode, ext: str) -> str:
    """Build a standardized short-drama episode filename."""
    suffix = ext if not ext or ext.startswith(".") else f".{ext}"
    number = episode_number(episode) or 0
    parts = [
        episode.series,
        f"EP{number:02d}",
    ]
    if episode.title:
        parts.append(episode.title)

    return sanitize_filename("_".join(parts) + suffix)


def _extension_from_mime(mime_type: str | None) -> str:
    """Return a conservative file extension from MIME type."""
    mime = (mime_type or "").lower()
    explicit = {
        "application/pdf": ".pdf",
        "application/epub+zip": ".epub",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
    }
    if mime in explicit:
        return explicit[mime]
    return mimetypes.guess_extension(mime) or ""


def media_extension(message: Message, media: Any | None = None) -> str:
    """Guess the right extension for media without a Telegram file_name."""
    media = media or get_message_media(message)
    if not media:
        return ""

    filename = getattr(media, "file_name", None)
    if filename:
        return Path(filename).suffix

    if getattr(message, "photo", None):
        return ".jpg"
    if getattr(message, "video", None) or getattr(message, "animation", None):
        return ".mp4"
    if getattr(message, "video_note", None):
        return ".mp4"
    if getattr(message, "voice", None):
        return ".ogg"
    if getattr(message, "audio", None):
        return _extension_from_mime(getattr(media, "mime_type", None)) or ".mp3"

    return _extension_from_mime(getattr(media, "mime_type", None))


def is_short_drama_episode_media(message: Message, media: Any | None = None) -> bool:
    """Return True for video-like media that can consume a catalog episode."""
    media = media or get_message_media(message)
    if not media:
        return False

    if (
        getattr(message, "video", None)
        or getattr(message, "animation", None)
        or getattr(message, "video_note", None)
    ):
        return True

    document = getattr(message, "document", None)
    if document:
        mime_type = (getattr(document, "mime_type", None) or "").lower()
        if mime_type.startswith("video/"):
            return True

        suffix = media_extension(message, document).lower()
        return suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

    return False


def build_catalog_filename(series: str, message_id: int, ext: str) -> str:
    """Build a standardized filename for a catalog image/media message."""
    suffix = ext if not ext or ext.startswith(".") else f".{ext}"
    return sanitize_filename(f"{series}_目录_{message_id}{suffix}")


def build_fallback_filename(message: Message, media: Any | None = None) -> str:
    """Build a human-friendly fallback filename from caption/message metadata."""
    media = media or get_message_media(message)
    ext = media_extension(message, media)
    caption = get_caption_text(message)
    episode = parse_caption_episode(caption)

    if getattr(message, "photo", None):
        if episode:
            return build_catalog_filename(episode.series, message.id, ext or ".jpg")
        if caption:
            return f"{caption}{ext or '.jpg'}"
        return f"message_{message.id}{ext or '.jpg'}"

    if caption:
        if episode:
            return build_episode_filename(episode, ext)
        return f"{caption}{ext}"

    return f"message_{message.id}{ext}"


def get_media_filename(message: Message, media: Any | None = None) -> str:
    """Return Telegram file_name or a caption/message based fallback."""
    media = media or get_message_media(message)
    if not media:
        return f"message_{message.id}"
    return getattr(media, "file_name", None) or build_fallback_filename(message, media)
