"""The backstop's safety property: recover the coin, or record nothing.

27% of graduations never reach the WebSocket. They are recoverable because the
label anchor is the AMM pool's creation timestamp, not our detection time — but
that is exactly why this code is dangerous. Anchoring on poll time would pass the
120s gate while measuring from a post-dump price, which is how the August outage
produced a 25.1% survival rate against a true 5.2%.
"""

from scripts.graduation_backstop import (
    ANCHOR_WINDOW_S, MAX_RECOVERY_AGE_S, POLL_INTERVAL_S, PUMP_MARKETS,
    _true_graduation_ts,
)


def test_anchor_window_matches_the_eval_gate():
    """If these drift apart the backstop admits coins the trainer then discards,
    or worse, rejects coins it would have accepted."""
    from eval._common import MAX_ANCHOR_LAG_S
    assert ANCHOR_WINDOW_S == MAX_ANCHOR_LAG_S


def test_true_graduation_comes_from_the_pool_not_the_clock():
    pools = [{"market": "pumpfun-amm", "createdAt": 1786976045183}]
    assert _true_graduation_ts(pools) == 1786976045


def test_non_pump_pools_are_not_a_graduation_source():
    """The feed carries other venues; a meteora pool is not our graduation."""
    assert _true_graduation_ts([{"market": "meteora-dyn-v2", "createdAt": 1786976045183}]) is None


def test_missing_pool_timestamp_yields_no_anchor():
    """No timestamp means no verifiable zero point — recovery must not proceed."""
    assert _true_graduation_ts([{"market": "pumpfun-amm"}]) is None
    assert _true_graduation_ts([]) is None


def test_poll_interval_is_inside_the_reach_budget():
    """The trade API pages newest-first within ~1500 trades, so the walk must span
    from now back to graduation. Measured: 92% of coins are still within reach at
    5 minutes, 77% at 10, 47% at 60."""
    assert POLL_INTERVAL_S <= 600


def test_recovery_age_cap_is_bounded():
    """Beyond roughly an hour the tape cannot be walked back and every attempt
    spends API budget to produce an anchor-miss."""
    assert MAX_RECOVERY_AGE_S <= 7200


def test_only_pump_markets_are_in_scope():
    assert PUMP_MARKETS == {"pumpfun", "pumpfun-amm"}
