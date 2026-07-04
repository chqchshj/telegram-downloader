"""Runtime short-drama catalog naming helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.media import (
    build_catalog_filename,
    get_caption_text,
    get_message_catalog_episodes,
    get_message_media,
    is_short_drama_episode_media,
    media_extension,
    parse_caption_episodes,
)
from src.state import ShortDramaAssignment, ShortDramaState

if TYPE_CHECKING:
    from pyrogram.types import Message


def plan_short_drama_assignments(
    messages: list["Message"],
    cursor_key: str,
    short_drama_state: ShortDramaState,
) -> dict[int, ShortDramaAssignment]:
    """Detect catalogs and assign runtime short-drama names in message order."""
    assignments: dict[int, ShortDramaAssignment] = {}

    for msg in sorted(messages, key=lambda message: message.id):
        media = get_message_media(msg)
        caption_episodes = parse_caption_episodes(get_caption_text(msg))

        if caption_episodes and is_short_drama_episode_media(msg, media):
            ext = media_extension(msg, media)
            assignments[msg.id] = short_drama_state.assign_episode(
                cursor_key,
                msg.id,
                caption_episodes[0],
                ext,
                catalog_message_id=msg.id,
            )
            continue

        catalog_episodes = get_message_catalog_episodes(msg)
        if catalog_episodes:
            catalog = short_drama_state.set_catalog(
                cursor_key,
                msg.id,
                catalog_episodes,
            )
            if media:
                ext = media_extension(msg, media)
                if getattr(msg, "photo", None) and not ext:
                    ext = ".jpg"
                filename = build_catalog_filename(catalog.series, msg.id, ext)
                assignments[msg.id] = short_drama_state.assign_catalog_media(
                    cursor_key,
                    msg.id,
                    filename,
                    catalog.series,
                    msg.id,
                )
            continue

        if is_short_drama_episode_media(msg, media):
            ext = media_extension(msg, media)
            assignment = short_drama_state.assign_next_episode(cursor_key, msg.id, ext)
            if assignment:
                assignments[msg.id] = assignment

    return assignments
