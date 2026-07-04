"""
Extension-based file filtering.

Filters messages based on file extensions, extracted from the
current downloader.py looks_like_ebook logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable
from src.filters.base import BaseFilter
from src.media import get_media_filename, get_message_media

if TYPE_CHECKING:
    from pyrogram.types import Message


def _endswith_any(name: str, exts: Iterable[str]) -> bool:
    """
    Check if filename ends with any of the given extensions.

    Comparison is case-insensitive for maximum compatibility.

    Args:
        name: Filename to check
        exts: List of extensions (e.g., [".epub", ".pdf"])

    Returns:
        True if filename ends with any extension
    """
    return any(name.lower().endswith(e.lower()) for e in exts)


class ExtensionFilter(BaseFilter):
    """
    Filter messages by file extension.

    Supports two categories of extensions:
    - ebook_exts: Primary file types to download (e.g., .epub, .pdf, .mobi)
    - archive_exts: Archive formats (e.g., .zip, .rar), only if allow_archives=True

    All extension matching is case-insensitive for user convenience.
    """

    def __init__(
        self,
        ebook_exts: list[str],
        allow_archives: bool,
        archive_exts: list[str]
    ):
        """
        Initialize extension filter.

        Args:
            ebook_exts: List of ebook extensions (e.g., [".epub", ".pdf"])
            allow_archives: Whether to allow archive formats
            archive_exts: List of archive extensions (e.g., [".zip", ".rar"])
        """
        # Store as lowercase for case-insensitive matching
        self.ebook_exts = [ext.lower() for ext in ebook_exts]
        self.allow_archives = allow_archives
        self.archive_exts = [ext.lower() for ext in archive_exts]

    async def matches(self, message: Message) -> bool:
        """
        Check if message contains media with allowed extension.

        Args:
            message: Pyrogram Message to evaluate

        Returns:
            True if message has media with allowed extension
        """
        media_obj = get_message_media(message)

        if not media_obj:
            return False

        # Extract filename
        fname = get_media_filename(message, media_obj)

        # Check against allowed extensions
        if _endswith_any(fname, self.ebook_exts):
            return True

        if self.allow_archives and _endswith_any(fname, self.archive_exts):
            return True

        return False

    def _generate_fallback_filename(self, message: Message, media_obj) -> str:
        """
        Generate fallback filename when file_name is missing.

        Uses MIME type to guess appropriate extension.

        Args:
            message: Pyrogram Message
            media_obj: Media object from message

        Returns:
            Generated filename with extension
        """
        return get_media_filename(message, media_obj)
