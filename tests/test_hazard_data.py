"""v5 hazard data layer — leak discipline + the delayed-stopping-time collapse event."""

from src.analyzer.hazard_data import (
    GRID_EDGES, collapse_event, expand_coin, robust_anchor,
)


def tape_row(t, side="buy", signer="w", sol=1.0, price=1.0):
    return (t, side, signer, sol, price)


# ── collapse event (delayed stopping time) ────────────────────────────────────────

def test_anchor_is_robust_to_one_bad_first_print():
    """A lone bad first print must not set the anchor (the single-print artifact)."""
    prices = [(1, 100.0), (2, 1.0), (3, 1.0), (4, 1.0)]
    assert robust_anchor(prices) == 1.0            # median of first 3, not 100


def test_collapse_confirmed_by_local_prints():
    prices = [(0, 1.0), (1, 1.0), (2, 1.0), (10, 0.4), (20, 0.4), (30, 0.4)]
    assert collapse_event(prices) == 10.0          # stamped at the CANDIDATE


def test_single_bad_print_does_not_collapse():
    prices = [(0, 1.0), (1, 1.0), (2, 1.0), (10, 0.1), (20, 1.0), (30, 1.0), (40, 1.0)]
    assert collapse_event(prices) is None          # voided by sustained recovery


def test_recovery_voids_then_search_restarts():
    """Dip -> recovery -> REAL collapse later: event at the later candidate, never
    backdated to the voided dip (trajectory.py's whole-tape mirror backdates)."""
    prices = ([(0, 1.0), (1, 1.0), (2, 1.0)]
              + [(10, 0.4), (15, 1.0), (16, 1.0), (17, 1.0)]      # voided dip
              + [(100, 0.3), (110, 0.3), (120, 0.3)])             # real collapse
    assert collapse_event(prices) == 100.0


def test_confirmation_window_bounds_lookahead():
    """Sub-threshold prints spread WIDER than W must not confirm."""
    prices = [(0, 1.0), (1, 1.0), (2, 1.0), (10, 0.4), (200, 0.4), (400, 0.4)]
    assert collapse_event(prices, w=120) is None


# ── expansion: competing risks + leak discipline ─────────────────────────────────

def test_grid_matches_live_checkpoints():
    from src.analyzer.distribution import EARLY_CHECK_SECONDS
    assert set(EARLY_CHECK_SECONDS) <= set(GRID_EDGES)     # every checkpoint = edge
    assert GRID_EDGES[-1] == 3600                          # administrative censoring


def test_exit_event_and_risk_set():
    tape = [tape_row(1), tape_row(2, price=1.0), tape_row(3, price=1.0),
            tape_row(45, "sell", "team")]
    A, B, coll = expand_coin("m", tape, {"team"}, 3600, 45)
    assert coll is None
    assert [r.t_start for r in A] == [0, 30]
    assert [r.event for r in A] == [0, 1]
    assert all(r.event == 0 for r in B)            # no collapse -> B rows event-free


def test_collapse_removes_coin_from_A_risk_set():
    """Collapse at ~61s with NO team sell: A-rows must stop at the collapse
    interval (competing event), not continue as if the team could still exit."""
    tape = ([tape_row(1), tape_row(2), tape_row(3)]
            + [tape_row(61, price=0.3), tape_row(62, price=0.3), tape_row(63, price=0.3)])
    A, B, coll = expand_coin("m", tape, {"team"}, 3600, None)
    assert coll == 61.0
    assert max(r.t_start for r in A) <= 60          # off A's risk set at collapse
    b_ev = [r for r in B if r.event]
    assert len(b_ev) == 1 and b_ev[0].t_start == 60  # collapse lands in [60,90)


def test_tv_covariates_strictly_before_interval_start():
    tape = [tape_row(10, price=1.0), tape_row(11, price=1.0), tape_row(12, price=1.0),
            tape_row(35, price=9.0)]
    A, _, _ = expand_coin("m", tape, set(), 3600, 50)
    r30 = next(r for r in A if r.t_start == 30)
    assert r30.tv_trades == 3.0                    # t=35 print is INVISIBLE
    assert r30.tv_price_vs_first == 1.0


def test_b_rows_carry_exit_state():
    tape = [tape_row(1), tape_row(2), tape_row(3),
            tape_row(20, "sell", "team", sol=5.0)]
    _, B, _ = expand_coin("m", tape, {"team"}, 3600, 20)
    r30 = next(r for r in B if r.t_start == 30)
    assert r30.b_team_exited == 1.0
    assert r30.b_team_sellers == 1.0
    r0 = next(r for r in B if r.t_start == 0)
    assert r0.b_team_exited == 0.0                 # not yet exited at interval 0


def test_censoring_requires_full_interval_observation():
    tape = [tape_row(1), tape_row(2), tape_row(3)]
    A, B, _ = expand_coin("m", tape, set(), 100, None)
    assert max(r.t_end for r in A) <= 100
    assert max(r.t_end for r in B) <= 100


# ── landmark scoring (live checkpoint path) ──────────────────────────────────────

def test_landmark_row_leak_discipline_and_exit_state():
    from src.analyzer.hazard_data import landmark_row
    tape = [tape_row(1), tape_row(2), tape_row(3),
            tape_row(50, "sell", "team", sol=2.0),
            tape_row(125, price=9.0)]                  # future print, must be invisible
    pp = landmark_row(tape, {"team"}, 120)
    assert pp is not None and pp.t_start == 120
    assert pp.tv_trades == 4.0                         # t=125 excluded
    assert pp.tv_price_vs_first == 1.0                 # 9x spike invisible
    assert pp.b_team_exited == 1.0 and pp.b_team_sellers == 1.0


def test_landmark_row_rejects_non_grid_checkpoints():
    from src.analyzer.hazard_data import landmark_row
    assert landmark_row([tape_row(1)], set(), 100) is None      # not a grid edge
    assert landmark_row([tape_row(1)], set(), 3600) is None     # terminal edge


def test_hazard_verdict_fail_safe_without_artifact(monkeypatch):
    import src.strategy.hazard_verdict as hv
    monkeypatch.setattr(hv, "_ARTIFACT", hv._ARTIFACT.with_name("nope.pkl"))
    monkeypatch.setattr(hv, "_cache", None)
    monkeypatch.setattr(hv, "_load_failed", False)
    assert hv.score_t0({"team_supply_pct": 10}) is None          # never raises
