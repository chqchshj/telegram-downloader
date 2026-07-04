"""Helpers for Telegram media detection and fallback naming."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    match = re.search(
        r"^\s*(?P<series>.+?)\s+(?P<episode>EP\s*[-_ ]?\s*\d+)\s*(?P<title>.*)$",
        caption,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    episode = re.sub(r"\s+", "", match.group("episode").upper())
    episode = episode.replace("_", "-")
    if not episode.startswith("EP-"):
        episode = episode.replace("EP", "EP-", 1)

    return CaptionEpisode(
        series=match.group("series").strip(),
        episode=episode,
        title=match.group("title").strip(),
    )


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


def build_fallback_filename(message: Message, media: Any | None = None) -> str:
    """Build a human-friendly fallback filename from caption/message metadata."""
    media = media or get_message_media(message)
    ext = media_extension(message, media)
    caption = get_caption_text(message)
    episode = parse_caption_episode(caption)

    if getattr(message, "photo", None):
        if episode:
            return f"{episode.series}_目录_{message.id}{ext or '.jpg'}"
        if caption:
            return f"{caption}{ext or '.jpg'}"
        return f"message_{message.id}{ext or '.jpg'}"

    if caption:
        if episode:
            title = f"_{episode.title}" if episode.title else ""
            return f"{episode.series}_{episode.episode}{title}{ext}"
        return f"{caption}{ext}"

    return f"message_{message.id}{ext}"


def get_media_filename(message: Message, media: Any | None = None) -> str:
    """Return Telegram file_name or a caption/message based fallback."""
    media = media or get_message_media(message)
    if not media:
        return f"message_{message.id}"
    return getattr(media, "file_name", None) or build_fallback_filename(message, media)
