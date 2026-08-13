"""wallet_watcher — the hot-loop guards.

Context: this service made 40.3 million consecutive rate-limited calls, wrote a
7.9 GB log, and produced zero alerts in its last 200k lines. Three defects
combined: unbounded concurrency, no backoff, and a new HTTP client per wallet.
"""

import asyncio

import pytest

from src.services import wallet_watcher as ww


class _Resp:
    def __init__(self, status): self.status_code = status


def _err(status):
    e = Exception("boom"); e.response = _Resp(status); return e


def test_rate_limit_is_recognised_by_status():
    assert ww._is_rate_limit(_err(429))
    assert ww._is_rate_limit(_err(503))


def test_other_failures_are_not_mistaken_for_rate_limits():
    """A 500 must surface as a bug, not be silently absorbed as backoff."""
    assert not ww._is_rate_limit(_err(500))
    assert not ww._is_rate_limit(Exception("connection reset"))


def test_rate_limit_recognised_from_message_when_no_response_attached():
    assert ww._is_rate_limit(Exception("HTTP 429 Too Many Requests"))


def test_concurrency_is_capped():
    """Unbounded gather over every smart-money wallet is what triggered the 429
    storm in the first place."""
    assert ww.MAX_CONCURRENT <= 8


def test_backoff_grows_and_is_bounded():
    assert ww.BACKOFF_START_S >= 30
    assert ww.BACKOFF_MAX_S >= ww.BACKOFF_START_S * 4


def test_poll_wallet_reuses_the_caller_client():
    """It used to open a new client per wallet while ignoring the one passed in,
    so a sweep shared no connection pool or rate-limit state."""
    calls = []

    class Client:
        async def get_transactions_for_address(self, addr, limit=20):
            calls.append(addr); return []

    w = type("W", (), {"address": "abc", "smart_money_score": 1.0})()
    asyncio.run(ww.poll_wallet(w, Client()))
    assert calls == ["abc"]


def test_poll_wallet_converts_rate_limit_to_signal():
    class Client:
        async def get_transactions_for_address(self, addr, limit=20):
            raise _err(429)

    w = type("W", (), {"address": "abc", "smart_money_score": 1.0})()
    with pytest.raises(ww.RateLimited):
        asyncio.run(ww.poll_wallet(w, Client()))


def test_watchlist_is_capped():
    """get_smart_money_wallets returns ~17,891 wallets; polling all of them each
    minute is ~298 req/s, which no tier serves."""
    assert ww.MAX_WATCHED <= 500


def test_a_sweep_fits_inside_the_poll_interval():
    """If a sweep cannot finish within POLL_INTERVAL, the post-sweep rate-limit
    check never runs and backoff never engages — which is how the service stayed
    a no-op while still burning requests."""
    worst_case_s = ww.MAX_WATCHED / ww.MAX_CONCURRENT   # >=1 req/s per slot
    assert worst_case_s <= ww.POLL_INTERVAL


def test_sweep_aborts_on_a_run_of_rate_limits():
    assert 1 <= ww.ABORT_AFTER_CONSECUTIVE_LIMITS <= ww.MAX_WATCHED
