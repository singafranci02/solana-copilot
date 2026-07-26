"""Person-period expansion — the leak discipline is the whole point of these tests."""

from src.analyzer.hazard_data import DEFAULT_EDGES, expand_coin


def tape_row(t, side="buy", signer="w", sol=1.0, price=1.0):
    return (t, side, signer, sol, price)


def test_event_interval_and_risk_exit():
    """Exit at 45s -> risk rows for [0,30) and [30,60) with event on the second,
    nothing after (off the risk set)."""
    tape = [tape_row(1), tape_row(40)]
    rows = expand_coin("m", tape, {"team"}, tape_span_s=3600, exit_offset_s=45)
    assert [r.t_start for r in rows] == [0, 30]
    assert [r.event for r in rows] == [0, 1]


def test_censoring_drops_unobserved_intervals():
    """Tape ends at 100s with no exit -> only fully-observed intervals contribute."""
    rows = expand_coin("m", [tape_row(1)], set(), tape_span_s=100, exit_offset_s=None)
    assert [r.t_start for r in rows] == [0, 30, 60]      # [90,120) not fully observed
    assert all(r.event == 0 for r in rows)


def test_covariates_strictly_before_interval_start():
    """A trade INSIDE the predicted interval must not appear in that row's covariates.
    This is the leak class that produced the fake 0.746 'pump predictor'."""
    tape = [tape_row(10, price=1.0), tape_row(35, price=9.0)]   # spike inside [30,60)
    rows = expand_coin("m", tape, set(), tape_span_s=3600, exit_offset_s=50)
    r30 = next(r for r in rows if r.t_start == 30)
    assert r30.tv_trades == 1.0                # only the t=10 trade
    assert r30.tv_price_vs_first == 1.0        # the 9x spike at t=35 is INVISIBLE


def test_instant_exit_yields_single_event_row():
    """38% of teams sell in the first 30s — those coins are one row, event=1."""
    rows = expand_coin("m", [tape_row(1)], {"t"}, tape_span_s=3600, exit_offset_s=5)
    assert len(rows) == 1 and rows[0].event == 1 and rows[0].t_start == 0


def test_net_flow_uses_previous_interval_only():
    tape = [tape_row(5, "buy", sol=10.0), tape_row(35, "sell", sol=4.0),
            tape_row(65, "buy", sol=100.0)]
    rows = expand_coin("m", tape, set(), tape_span_s=3600, exit_offset_s=200)
    r60 = next(r for r in rows if r.t_start == 60)
    assert r60.tv_net_flow_recent == -4.0      # only the [30,60) window; t=65 invisible
