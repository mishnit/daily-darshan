from datetime import datetime, timezone

from domain.clock import today_ist


def test_utc_evening_is_next_business_day_in_india():
    utc_evening = datetime(2026, 9, 13, 18, 45, tzinfo=timezone.utc)

    assert today_ist(utc_evening).isoformat() == "2026-09-14"
