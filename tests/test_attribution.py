"""Team-exit attribution: 'held' must mean measured, not unobserved."""

import json
import sqlite3

from src.analyzer.attribution import (
    HELD, OBSERVED, SUPPLY_TRUST_PCT, UNCERTAIN, classify_attribution,
)


def _db(exit_s, supply, members, bc_buyers, sellers):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE coin_trajectory (token_mint TEXT, time_to_team_exit_s REAL, n_price_points INT)")
    c.execute("CREATE TABLE team_clusters (token_mint TEXT, member_addresses TEXT, supply_pct_at_graduation REAL)")
    c.execute("CREATE TABLE token_buyers (token_mint TEXT, wallet_address TEXT)")
    c.execute("CREATE TABLE post_grad_swaps (token_mint TEXT, wallet_address TEXT, side TEXT)")
    c.execute("INSERT INTO coin_trajectory VALUES ('M',?,100)", (exit_s,))
    c.execute("INSERT INTO team_clusters VALUES ('M',?,?)", (json.dumps(members), supply))
    c.executemany("INSERT INTO token_buyers VALUES ('M',?)", [(w,) for w in bc_buyers])
    c.executemany("INSERT INTO post_grad_swaps VALUES ('M',?,'sell')", [(w,) for w in sellers])
    return c


def test_measured_exit_is_observed():
    assert classify_attribution(_db(42.0, 5.0, ["a"], ["a"], ["a"]), "M")[0] == OBSERVED


def test_no_seller_anywhere_is_a_real_hold():
    assert classify_attribution(_db(None, 5.0, ["a"], ["a", "b"], []), "M")[0] == HELD


def test_ungated_buyer_selling_makes_it_uncertain():
    """The gated team shows nothing, but a BC buyer outside it sold — we may
    simply have failed to attribute that wallet to the team."""
    state, n = classify_attribution(_db(None, 5.0, ["a"], ["a", "b"], ["b"]), "M")
    assert state == UNCERTAIN and n == 1


def test_a_gated_member_selling_does_not_trigger_uncertainty():
    """Only wallets OUTSIDE the gated team count as evidence of a blind spot."""
    assert classify_attribution(_db(None, 5.0, ["a"], ["a"], ["a"]), "M")[0] == HELD


def test_high_supply_teams_are_trusted_without_the_scan():
    """Above the supply threshold the blind spot is 4%, so absence is evidence."""
    state, _ = classify_attribution(
        _db(None, SUPPLY_TRUST_PCT + 1, ["a"], ["a", "b"], ["b"]), "M")
    assert state == HELD


def test_missing_coin_is_uncertain_not_held():
    """Fail closed: an unknown coin must never be reported as loyal."""
    c = _db(None, 5.0, ["a"], [], [])
    c.execute("DELETE FROM coin_trajectory")
    assert classify_attribution(c, "M")[0] == UNCERTAIN
