"""
Source abstraction layer for multi-source media downloads.

Provides unified interface for iterating media from different
Telegram source types (forum topics, channels, groups, private chats).
"""

from src.sources.base import BaseSource

__all__ = [
    "BaseSource",
    "ForumTopicSource",
    "ChannelSource",
    "GroupSource",
    "PrivateChatSource",
    "create_source",
]


def __getattr__(name):
    """Lazily import Telegram-dependent source implementations."""
    if name == "ForumTopicSource":
        from src.sources.forum_topic import ForumTopicSource

        return ForumTopicSource
    if name == "ChannelSource":
        from src.sources.channel import ChannelSource

        return ChannelSource
    if name == "GroupSource":
        from src.sources.group import GroupSource

        return GroupSource
    if name == "PrivateChatSource":
        from src.sources.private_chat import PrivateChatSource

        return PrivateChatSource
    if name == "create_source":
        from src.sources.factory import create_source

        return create_source
    raise AttributeError(name)
