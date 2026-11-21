"""Times in text output: local time with the zone, or a duration; never a raw UTC stamp. JSON keeps its stamps."""

import os
import time
from datetime import UTC, datetime

import pytest

from steuermaschine import localtime


@pytest.fixture
def berlin():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ")
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_utc_stamps_become_local_time_with_the_zone(berlin):
    assert localtime.localize("run at 2026-11-21T09:05:12Z done") == "run at 2026-11-21 10:05 CET done"
    assert localtime.localize("2026-07-01T09:05:12.345+00:00") == "2026-07-01 11:05 CEST"
    assert localtime.localize("2026-11-21 09:05:12+00:00 and 2026-11-21T11:05:00+0200") == "2026-11-21 10:05 CET and 2026-11-21 10:05 CET"


def test_stamps_without_an_offset_and_other_text_stay(berlin):
    for text in ("2026-11-21T09:05:12", "version 0.9.53 on 2026-11-21", "sha 2026-11-21T09:05:12Z0"[:0] + "plain"):
        assert localtime.localize(text) == text


def test_durations():
    assert [localtime.duration(s) for s in (-5, 59, 300, 3 * 3600 + 20 * 60, 2 * 86400 + 4 * 3600)] == ["0m", "0m", "5m", "3h 20m", "2d 4h"]
    assert localtime.since("2026-11-21T09:00:00Z", datetime(2026, 11, 21, 12, 30, tzinfo=UTC)) == "3h 30m"
