import time

import pytest

from bot.rate_limiter import ConcurrencyGuard, RateLimiter


def test_rate_limiter_allows_up_to_limit():
    rl = RateLimiter(max_per_minute=3)
    assert rl.check(1) is True
    assert rl.check(1) is True
    assert rl.check(1) is True
    assert rl.check(1) is False


def test_rate_limiter_isolates_keys():
    rl = RateLimiter(max_per_minute=2)
    assert rl.check(100) is True
    assert rl.check(100) is True
    # Different chat_id is not affected
    assert rl.check(200) is True
    assert rl.check(100) is False


def test_rate_limiter_sliding_window(monkeypatch):
    rl = RateLimiter(max_per_minute=2)
    t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    assert rl.check(1) is True
    assert rl.check(1) is True
    assert rl.check(1) is False
    t[0] += 61  # advance past the window
    assert rl.check(1) is True


def test_rate_limiter_reject_invalid_limit():
    with pytest.raises(ValueError):
        RateLimiter(max_per_minute=0)


@pytest.mark.asyncio
async def test_concurrency_guard_blocks_over_limit():
    guard = ConcurrencyGuard(limit=2)
    async with guard:
        async with guard:
            # third acquisition must wait; we don't actually wait here because it
            # would deadlock. Instead assert that semaphore's value is 0.
            assert guard._sem._value == 0
