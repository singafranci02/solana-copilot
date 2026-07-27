"""Ops monitor — the anti-spam state machine and pure thresholds are the contract."""

from scripts.ops_monitor import feed_gap_is_alarming, should_alert, st_pace_exceeds_budget

T = 1_000_000


def test_feed_gap_scales_with_flow():
    assert not feed_gap_is_alarming(180, grads_last_24h=30)   # 3h at healthy flow: fine
    assert feed_gap_is_alarming(300, grads_last_24h=30)       # 5h at healthy flow: alarm
    assert not feed_gap_is_alarming(300, grads_last_24h=2)    # quiet market: patience
    assert feed_gap_is_alarming(800, grads_last_24h=2)        # 13h: alarm even if quiet


def test_st_budget_projection():
    assert not st_pace_exceeds_budget(50_000, day_of_month=15)   # -> 100k/mo: fine
    assert st_pace_exceeds_budget(150_000, day_of_month=15)      # -> 300k/mo: alarm
    assert not st_pace_exceeds_budget(40_000, day_of_month=2)    # grace early in month


def test_alert_once_then_cooldown_then_realert():
    a, s = should_alert(None, ok=False, now=T)
    assert a == "alert"
    a2, s = should_alert(s, ok=False, now=T + 600)               # 10 min later: silent
    assert a2 is None
    a3, s = should_alert(s, ok=False, now=T + 5 * 3600)          # past cooldown
    assert a3 == "realert"


def test_recovery_fires_once():
    _, s = should_alert(None, ok=False, now=T)
    a, s = should_alert(s, ok=True, now=T + 100)
    assert a == "recovered"
    a2, _ = should_alert(s, ok=True, now=T + 200)                # stays quiet after
    assert a2 is None


def test_healthy_never_alerts():
    a, s = should_alert(None, ok=True, now=T)
    assert a is None
    a2, _ = should_alert(s, ok=True, now=T + 999_999)
    assert a2 is None
