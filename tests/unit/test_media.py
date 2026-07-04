"""Tests for shared Telegram media helpers."""
from tests.conftest import MockMessage, MockPhoto

from src.media import get_media_filename, parse_caption_episode


def test_parse_caption_episode_with_chinese_series():
    episode = parse_caption_episode("美丽新世界 EP-1 樱花道偶遇")

    assert episode is not None
    assert episode.series == "美丽新世界"
    assert episode.episode == "EP-1"
    assert episode.title == "樱花道偶遇"


def test_photo_uses_series_index_fallback_name():
    message = MockMessage(
        id=42,
        document=None,
        photo=MockPhoto(),
        caption="美丽新世界 EP-1 樱花道偶遇",
    )

    assert get_media_filename(message) == "美丽新世界_目录_42.jpg"
