import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Claude uses rolling 5h windows. If we can't parse an exact reset time
# from the error, we fall back to now + DEFAULT_RESET_HOURS.
DEFAULT_RESET_HOURS = 5

_LIMIT_PATTERNS = [
    r"usage limit",
    r"rate limit",
    r"limit reached",
    r"limit\s+(?:\w+\s+){0,3}reset",  # "limit reset", "limit will reset", etc.
    r"quota exceeded",
    r"too many requests",
    r"rate_limit_error",
    r"usage_limit_exceeded",
]
_LIMIT_RE = re.compile("|".join(_LIMIT_PATTERNS), re.IGNORECASE)


def is_limit_error(text: str | None) -> bool:
    """Detect whether an error message indicates a provider usage/rate limit."""
    if not text:
        return False
    return bool(_LIMIT_RE.search(text))


_IN_DURATION_RE = re.compile(
    r"\bin\s+(?:(\d+)\s*h(?:our)?s?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?",
    re.IGNORECASE,
)
_AT_CLOCK_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE,
)


def parse_reset_time(text: str | None, now: datetime | None = None) -> datetime:
    """Best-effort parse of a reset time from a limit error message.

    Returns a timezone-aware UTC datetime. Falls back to `now + DEFAULT_RESET_HOURS`
    if no pattern matches.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not text:
        return now + timedelta(hours=DEFAULT_RESET_HOURS)

    # Try "in Xh Ym" / "in X hours" / "in Y minutes"
    m = _IN_DURATION_RE.search(text)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        if hours > 0 or minutes > 0:
            return now + timedelta(hours=hours, minutes=minutes)

    # Try "at 4pm" / "at 16:00" (assume today or tomorrow, same tz as now)
    m = _AT_CLOCK_RE.search(text)
    if m:
        try:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = (m.group(3) or "").lower()
            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(days=1)
                return candidate
        except (ValueError, TypeError):
            pass

    return now + timedelta(hours=DEFAULT_RESET_HOURS)
