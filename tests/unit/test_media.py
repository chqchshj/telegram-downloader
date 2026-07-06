"""Tests for shared Telegram media helpers."""
from datetime import datetime

from tests.conftest import MockDocument, MockMessage, MockPhoto

from src.media import (
    CaptionEpisode,
    build_episode_filename,
    get_media_filename,
    parse_caption_episode,
    parse_caption_episodes,
)


def test_parse_caption_episode_with_chinese_series():
    episode = parse_caption_episode("美丽新世界 EP-1 樱花道偶遇")

    assert episode is not None
    assert episode.series == "美丽新世界"
    assert episode.episode == "EP-1"
    assert episode.title == "樱花道偶遇"


def test_parse_caption_episodes_with_multiline_chinese_catalog():
    episodes = parse_caption_episodes(
        """
        🔥美丽新世界 EP-1 樱花道偶遇
        美丽新世界 EP2 误入厕所成变态
        美丽新世界 EP 12 烂醉如泥的邻居美眉
        """
    )

    assert episodes == [
        CaptionEpisode(series="美丽新世界", episode="EP-1", title="樱花道偶遇"),
        CaptionEpisode(series="美丽新世界", episode="EP-2", title="误入厕所成变态"),
        CaptionEpisode(series="美丽新世界", episode="EP-12", title="烂醉如泥的邻居美眉"),
    ]


def test_parse_caption_episode_with_chinese_episode_marker():
    episode = parse_caption_episode("归来仍少年 第01集 初见")

    assert episode == CaptionEpisode(series="归来仍少年", episode="EP-1", title="初见")


def test_parse_caption_episode_with_single_full_series_marker():
    episode = parse_caption_episode("限定心动 全1集 完整版")

    assert episode == CaptionEpisode(series="限定心动", episode="EP-1", title="完整版")


def test_build_episode_filename_preserves_chinese_and_pads_episode():
    filename = build_episode_filename(
        CaptionEpisode(series="美丽新世界", episode="EP-1", title="樱花道偶遇"),
        ".mp4",
    )

    assert filename == "美丽新世界_EP01_樱花道偶遇.mp4"


def test_build_episode_filename_sanitizes_dangerous_characters():
    filename = build_episode_filename(
        CaptionEpisode(series="美丽/新世界", episode="EP 2", title="误入:厕所?"),
        "mp4",
    )

    assert filename == "美丽_新世界_EP02_误入_厕所_.mp4"


def test_photo_uses_stable_date_message_fallback_name():
    message = MockMessage(
        id=42,
        document=None,
        photo=MockPhoto(),
        date=datetime(2026, 7, 5, 12, 0, 0),
        caption="美丽新世界 EP-1 樱花道偶遇",
    )

    assert get_media_filename(message) == "20260705_42.jpg"


def test_telegram_file_name_gets_archive_date_message_prefix():
    message = MockMessage(
        id=1907,
        document=MockDocument(
            file_name="7月1日 (1)(29).mp4",
            mime_type="video/mp4",
        ),
        date=datetime(2026, 7, 1, 12, 0, 0),
    )

    assert (
        get_media_filename(message, archive_date=datetime(2026, 7, 7, 9, 30, 0))
        == "20260707_msg1907_7月1日__1__29_.mp4"
    )


def test_fallback_without_telegram_file_name_is_not_archive_prefixed():
    document = MockDocument(file_name="", mime_type="video/mp4")
    message = MockMessage(
        id=1907,
        document=document,
        date=datetime(2026, 7, 1, 12, 0, 0),
    )

    assert get_media_filename(message, archive_date=datetime(2026, 7, 7)) == "20260701_1907.mp4"
