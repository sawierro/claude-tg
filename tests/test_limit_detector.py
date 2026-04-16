from datetime import datetime, timedelta, timezone

from bot import limit_detector


def test_is_limit_error_positive():
    assert limit_detector.is_limit_error("Claude usage limit reached")
    assert limit_detector.is_limit_error("rate_limit_error: try again later")
    assert limit_detector.is_limit_error("Too many requests, slow down")
    assert limit_detector.is_limit_error("your limit will reset at 4pm")


def test_is_limit_error_negative():
    assert not limit_detector.is_limit_error("Some random error")
    assert not limit_detector.is_limit_error("")
    assert not limit_detector.is_limit_error(None)


def test_parse_reset_time_default():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time("usage limit reached", now=now)
    # Falls back to +5h
    assert reset == now + timedelta(hours=limit_detector.DEFAULT_RESET_HOURS)


def test_parse_reset_time_in_duration():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time(
        "limit reached, try again in 2h 30m", now=now
    )
    assert reset == now + timedelta(hours=2, minutes=30)


def test_parse_reset_time_in_minutes_only():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time(
        "limit reset in 45 minutes", now=now
    )
    assert reset == now + timedelta(minutes=45)


def test_parse_reset_time_at_clock_future():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time(
        "limit resets at 16:30", now=now
    )
    assert reset.hour == 16
    assert reset.minute == 30
    assert reset.day == 1


def test_parse_reset_time_at_clock_past_rolls_next_day():
    now = datetime(2026, 1, 1, 20, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time(
        "limit resets at 10:00", now=now
    )
    assert reset.hour == 10
    assert reset.day == 2


def test_parse_reset_time_at_pm():
    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    reset = limit_detector.parse_reset_time(
        "limit will reset at 4pm", now=now
    )
    assert reset.hour == 16
