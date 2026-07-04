"""Unit tests for DateFilter."""
from datetime import datetime, timezone

import pytest

from src.filters.date import DateFilter
from tests.conftest import MockMessage


@pytest.mark.asyncio
async def test_naive_message_date_compares_with_aware_filter():
    filter = DateFilter(only_after=datetime(2026, 6, 18, tzinfo=timezone.utc))
    message = MockMessage(id=1, date=datetime(2026, 6, 19))

    assert await filter.matches(message) is True
