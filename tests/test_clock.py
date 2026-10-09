from datetime import datetime, timedelta, timezone

import pytest

from video_agent.clock import FrozenClock, SystemClock


def test_frozen_clock_stays_put_until_advanced():
    start = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
    clock = FrozenClock(start)
    assert clock.now() == start
    assert clock.now().isoformat() == "2026-10-09T03:00:00+00:00"
    clock.advance(5)
    assert clock.now() == start + timedelta(seconds=5)


def test_frozen_clock_rejects_naive_start():
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 10, 9, 3, 0))


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
