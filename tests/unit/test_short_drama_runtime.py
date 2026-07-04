"""Runtime short-drama naming tests without Telegram network access."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest

from src.config.schema import SourceConfig
from src.media import get_message_media
from src.nfo import write_short_drama_nfo
from src.organization.path_builder import build_destination_path
from src.short_drama import plan_short_drama_assignments
from src.state import ShortDramaAssignment, ShortDramaState
from tests.conftest import MockDocument, MockMessage, MockPhoto


CATALOG_A = "\n".join([
    "美丽新世界 EP-1 樱花道偶遇",
    "美丽新世界 EP-2 误入厕所成变态",
])

CATALOG_B = "\n".join([
    "下一部短剧 EP-1 初见",
    "下一部短剧 EP-2 重逢",
])


class MockSource:
    chat_id = -100123

    async def get_display_name(self) -> str:
        return "AI短剧"

    def get_cursor_key(self) -> str:
        return "channel:-100123"


async def _run_batch(
    temp_dir: Path,
    messages: list[MockMessage],
    short_drama_state: ShortDramaState,
) -> list[Path]:
    assignments = plan_short_drama_assignments(
        messages,
        "channel:-100123",
        short_drama_state,
    )

    paths: list[Path] = []
    for message in messages:
        assignment = assignments.get(message.id)
        if not assignment or not get_message_media(message):
            continue

        paths.append(
            await build_destination_path(
                temp_dir,
                MockSource(),
                assignment.filename,
                SourceConfig(url="https://t.me/test", name="AI短剧"),
                flat_structure=False,
                folder_override=assignment.folder_name,
            )
        )

    return paths


def _video_message(
    message_id: int,
    file_name: str,
    caption: str | None = None,
) -> MockMessage:
    return MockMessage(
        id=message_id,
        document=MockDocument(
            file_name=file_name,
            file_size=1024,
            mime_type="video/mp4",
            file_unique_id=f"unique_{message_id}",
        ),
        caption=caption,
    )


@pytest.mark.asyncio
async def test_catalog_then_generic_videos_use_series_episode_names_and_folder(
    temp_dir,
):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        paths = await _run_batch(
            temp_dir,
            [
                MockMessage(
                    id=10,
                    photo=MockPhoto(file_unique_id="catalog_a"),
                    caption=CATALOG_A,
                ),
                _video_message(11, "6_20__2_.mp4"),
                _video_message(12, "6_20__3_.mp4"),
            ],
            short_drama_state,
        )

    relative_paths = {path.relative_to(temp_dir) for path in paths}
    assert Path("美丽新世界/美丽新世界_目录_10.jpg") in relative_paths
    assert Path("美丽新世界/美丽新世界_EP01_樱花道偶遇.mp4") in relative_paths
    assert Path("美丽新世界/美丽新世界_EP02_误入厕所成变态.mp4") in relative_paths


@pytest.mark.asyncio
async def test_new_catalog_changes_runtime_series_folder(temp_dir):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        paths = await _run_batch(
            temp_dir,
            [
                MockMessage(id=20, text=CATALOG_A),
                _video_message(21, "first.mp4"),
                MockMessage(id=22, text=CATALOG_B),
                _video_message(23, "second.mp4"),
            ],
            short_drama_state,
        )

    relative_paths = {path.relative_to(temp_dir) for path in paths}
    assert Path("美丽新世界/美丽新世界_EP01_樱花道偶遇.mp4") in relative_paths
    assert Path("下一部短剧/下一部短剧_EP01_初见.mp4") in relative_paths


@pytest.mark.asyncio
async def test_persisted_catalog_state_maps_video_in_later_batch(
    temp_dir,
):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        first_paths = await _run_batch(
            temp_dir,
            [MockMessage(id=30, text=CATALOG_A)],
            short_drama_state,
        )

    assert first_paths == []

    with ShortDramaState(str(db_path)) as short_drama_state:
        second_paths = await _run_batch(
            temp_dir,
            [_video_message(31, "later.mp4")],
            short_drama_state,
        )

    assert [path.relative_to(temp_dir) for path in second_paths] == [
        Path("美丽新世界/美丽新世界_EP01_樱花道偶遇.mp4")
    ]


@pytest.mark.asyncio
async def test_single_line_catalog_state_maps_video_in_later_batch(temp_dir):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        first_paths = await _run_batch(
            temp_dir,
            [MockMessage(id=35, text="限定心动 第1集 完整版")],
            short_drama_state,
        )

    assert first_paths == []

    with ShortDramaState(str(db_path)) as short_drama_state:
        second_paths = await _run_batch(
            temp_dir,
            [_video_message(36, "later.mp4")],
            short_drama_state,
        )

    assert [path.relative_to(temp_dir) for path in second_paths] == [
        Path("限定心动/限定心动_EP01_完整版.mp4")
    ]


@pytest.mark.asyncio
async def test_video_with_own_single_episode_caption_is_assigned_directly(temp_dir):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        paths = await _run_batch(
            temp_dir,
            [_video_message(37, "generic.mp4", caption="归来仍少年 第01集 初见")],
            short_drama_state,
        )

    assert [path.relative_to(temp_dir) for path in paths] == [
        Path("归来仍少年/归来仍少年_EP01_初见.mp4")
    ]


@pytest.mark.asyncio
async def test_reprocessing_same_catalog_does_not_rewind_episode_index(temp_dir):
    db_path = temp_dir / "state.db"
    with ShortDramaState(str(db_path)) as short_drama_state:
        await _run_batch(
            temp_dir,
            [MockMessage(id=40, text=CATALOG_A), _video_message(41, "first.mp4")],
            short_drama_state,
        )

        paths = await _run_batch(
            temp_dir,
            [MockMessage(id=40, text=CATALOG_A), _video_message(42, "second.mp4")],
            short_drama_state,
        )

    assert [path.relative_to(temp_dir) for path in paths] == [
        Path("美丽新世界/美丽新世界_EP02_误入厕所成变态.mp4")
    ]


def test_short_drama_nfo_xml_is_generated(temp_dir):
    video_path = temp_dir / "限定心动" / "限定心动_EP01_完整版.mp4"
    video_path.parent.mkdir()
    assignment = ShortDramaAssignment(
        series="限定心动",
        filename=video_path.name,
        folder_name="限定心动",
        catalog_message_id=35,
        episode_index=0,
        episode_number=1,
        title="完整版",
    )
    message = MockMessage(id=36, date=datetime(2026, 7, 4, 9, 30))

    series_nfo, episode_nfo = write_short_drama_nfo(video_path, assignment, message)

    assert series_nfo == video_path.parent / "tvshow.nfo"
    assert episode_nfo == video_path.with_suffix(".nfo")

    tvshow_root = ElementTree.parse(series_nfo).getroot()
    assert tvshow_root.tag == "tvshow"
    assert tvshow_root.findtext("title") == "限定心动"

    episode_root = ElementTree.parse(episode_nfo).getroot()
    assert episode_root.tag == "episodedetails"
    assert episode_root.findtext("title") == "完整版"
    assert episode_root.findtext("showtitle") == "限定心动"
    assert episode_root.findtext("season") == "1"
    assert episode_root.findtext("episode") == "1"
    assert episode_root.findtext("plot") == "完整版"
    assert episode_root.findtext("aired") == "2026-07-04"
    assert episode_root.find("uniqueid").text == "36"
    assert episode_root.find("uniqueid").attrib == {"type": "telegram_message"}


def test_short_drama_nfo_does_not_overwrite_existing_files(temp_dir):
    video_path = temp_dir / "限定心动" / "限定心动_EP01_完整版.mp4"
    video_path.parent.mkdir()
    tvshow_nfo = video_path.parent / "tvshow.nfo"
    episode_nfo = video_path.with_suffix(".nfo")
    tvshow_nfo.write_text("custom series", encoding="utf-8")
    episode_nfo.write_text("custom episode", encoding="utf-8")
    assignment = ShortDramaAssignment(
        series="限定心动",
        filename=video_path.name,
        folder_name="限定心动",
        catalog_message_id=35,
        episode_number=1,
        title="完整版",
    )

    assert write_short_drama_nfo(video_path, assignment, MockMessage(id=36)) == (
        None,
        None,
    )
    assert tvshow_nfo.read_text(encoding="utf-8") == "custom series"
    assert episode_nfo.read_text(encoding="utf-8") == "custom episode"
