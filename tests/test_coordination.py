"""Tests for src/analyzer/coordination.py — pure functions only (offline)."""

from src.ingest.helius import Swap
from src.analyzer.coordination import (
    group_by_slot, compute_bundle_stats,
    edges_same_slot, edges_buy_size_fingerprint, edges_lockstep_sells, edges_shared_funder,
    assemble_entities, analyze_coin, fresh_flags, fresh_ratio,
)

MINT = "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _swap(signer, side="buy", sol=1.0, tokens=100.0, ts=1000, slot=500, mint=MINT):
    return Swap(side=side, token_mint=mint, sol_amount=sol, token_amount=tokens,
                signer=signer, timestamp=ts, slot=slot)


# ── group_by_slot / bundles ─────────────────────────────────────────────────────

def test_two_wallets_same_slot_is_bundle():
    swaps = [_swap("W1", slot=500), _swap("W2", slot=500)]
    bundles = group_by_slot(swaps, slot_window=0)
    assert len(bundles) == 1
    assert set(bundles[0].wallets) == {"W1", "W2"}


def test_same_wallet_twice_same_slot_not_bundle():
    swaps = [_swap("W1", slot=500), _swap("W1", slot=500)]
    assert group_by_slot(swaps) == []


def test_different_slots_not_bundle_when_window_zero():
    swaps = [_swap("W1", slot=500), _swap("W2", slot=501)]
    assert group_by_slot(swaps, slot_window=0) == []


def test_adjacent_slots_bundle_when_window_one():
    swaps = [_swap("W1", slot=500), _swap("W2", slot=501)]
    bundles = group_by_slot(swaps, slot_window=1)
    assert len(bundles) == 1


def test_sells_ignored_in_bundles():
    swaps = [_swap("W1", side="sell", slot=500), _swap("W2", side="sell", slot=500)]
    assert group_by_slot(swaps) == []


def test_bundle_stats_pct_with_known_supply():
    swaps = [_swap("W1", tokens=100, slot=500), _swap("W2", tokens=100, slot=500)]
    stats = compute_bundle_stats(swaps, total_supply=1000)
    assert stats.bundled_supply_pct == 20.0   # 200/1000
    assert stats.bundle_wallet_count == 2
    assert stats.largest_bundle_size == 2


def test_bundle_stats_pct_without_supply_uses_observed_volume():
    swaps = [_swap("W1", tokens=100, slot=500), _swap("W2", tokens=100, slot=500),
             _swap("W3", tokens=200, slot=999)]  # W3 alone, not bundled
    stats = compute_bundle_stats(swaps)  # denom = 400 observed; bundled = 200
    assert stats.bundled_supply_pct == 50.0


# ── edge sources ────────────────────────────────────────────────────────────────

def test_edges_same_slot_pairs():
    swaps = [_swap("W1", slot=5), _swap("W2", slot=5), _swap("W3", slot=5)]
    edges = edges_same_slot(swaps)
    assert edges == {("W1", "W2"), ("W1", "W3"), ("W2", "W3")}


def test_edges_buy_size_within_tolerance():
    swaps = [_swap("W1", sol=1.00), _swap("W2", sol=1.01)]  # within 2%
    assert ("W1", "W2") in edges_buy_size_fingerprint(swaps, rel_tol=0.02)


def test_edges_buy_size_outside_tolerance():
    swaps = [_swap("W1", sol=1.0), _swap("W2", sol=2.0)]
    assert edges_buy_size_fingerprint(swaps, rel_tol=0.02) == set()


def test_edges_lockstep_sells_within_window():
    swaps = [_swap("W1", side="sell", ts=1000), _swap("W2", side="sell", ts=1001)]
    assert ("W1", "W2") in edges_lockstep_sells(swaps, window_s=2)


def test_edges_lockstep_sells_outside_window():
    swaps = [_swap("W1", side="sell", ts=1000), _swap("W2", side="sell", ts=1010)]
    assert edges_lockstep_sells(swaps, window_s=2) == set()


def test_edges_shared_funder_excludes_cex():
    edges = edges_shared_funder({"W1": "cex", "W2": "cex"})
    assert edges == set()


def test_edges_shared_funder_links_same_funder():
    edges = edges_shared_funder({"W1": "FUNDER", "W2": "FUNDER", "W3": "OTHER"})
    assert edges == {("W1", "W2")}


# ── assemble_entities (union-find) ──────────────────────────────────────────────

def test_union_transitivity_three_wallets_one_entity():
    swaps = [_swap("A", tokens=100), _swap("B", tokens=100), _swap("C", tokens=100)]
    # A~B (same slot), B~C (funder) → one entity of 3
    edges = {("A", "B"), ("B", "C")}
    ents = assemble_entities(swaps, edges, total_supply=1000)
    assert len(ents) == 1
    assert ents[0].wallet_count == 3
    assert set(ents[0].wallets) == {"A", "B", "C"}


def test_singletons_excluded():
    swaps = [_swap("A"), _swap("B")]
    assert assemble_entities(swaps, set()) == []


def test_entity_id_deterministic():
    swaps = [_swap("A"), _swap("B")]
    e1 = assemble_entities(swaps, {("A", "B")})[0]
    e2 = assemble_entities(swaps, {("B", "A")})[0]
    assert e1.entity_id == e2.entity_id


def test_entity_state_distributing():
    swaps = [
        _swap("A", side="buy", tokens=100), _swap("B", side="buy", tokens=100),
        _swap("A", side="sell", tokens=80, slot=999),  # sold 80/200 = 40% > 30%
    ]
    ents = assemble_entities(swaps, {("A", "B")}, total_supply=1000)
    assert ents[0].state == "DISTRIBUTING"


# ── fresh scoring ────────────────────────────────────────────────────────────────

def test_fresh_flags_thresholds():
    now = 1_000_000
    flags = fresh_flags(
        wallet_first_seen={"new": now - 3600, "old": now - 10 * 86400, "warn": now - 2 * 86400},
        wallet_first_buy_offset={"old": 60},  # old bought 60s after launch → sniper
        now_ts=now,
    )
    assert flags["new"] == "critical"
    assert flags["warn"] == "warning"
    assert flags["old"] == "sniper"


def test_fresh_ratio():
    flags = {"A": "critical", "B": "none"}
    assert fresh_ratio(("A", "B"), flags) == 0.5


# ── analyze_coin orchestrator ────────────────────────────────────────────────────

def test_analyze_coin_end_to_end():
    swaps = [
        _swap("A", sol=1.0, tokens=100, slot=5), _swap("B", sol=1.0, tokens=100, slot=5),  # bundle
        _swap("C", sol=7.3, tokens=50, slot=999),  # solo: distinct slot AND distinct buy size
    ]
    cc = analyze_coin(MINT, swaps, total_supply=1000)
    assert cc.entity_count == 1
    assert cc.largest_entity_wallet_count == 2
    assert cc.bundle_stats.bundled_supply_pct == 20.0
    assert cc.largest_entity_supply_pct == 20.0


# ── typed edge persistence (2026-08-19) ──────────────────────────────────────
# analyze_coin computed the pairwise labeled edge list on every coin and discarded
# it: assemble_entities flattens it to a per-component union of label strings, so
# coordinated_entities recorded THAT a group was linked by {same_slot, funder} but
# never WHICH members the funder edge joined. These pin the recovered pairs.

import sqlite3


def test_analyze_coin_carries_the_typed_edges():
    sw = [_swap("aaa", slot=1), _swap("bbb", slot=1), _swap("ccc", slot=9)]
    cc = analyze_coin(MINT, sw, total_supply=1000.0)
    assert cc.edges, "the pairwise edge list must survive analyze_coin"
    assert ("aaa", "bbb") in cc.edges
    assert "same_slot" in cc.edges[("aaa", "bbb")]


def test_edges_are_canonically_ordered():
    """The DB CHECK relies on _pair() sorting a < b; a violation would fail the
    insert rather than store a mirrored duplicate."""
    sw = [_swap("zzz", slot=1), _swap("aaa", slot=1)]
    cc = analyze_coin(MINT, sw, total_supply=1000.0)
    for a, b in cc.edges:
        assert a < b


def test_a_pair_can_carry_several_signals():
    """Two wallets in the same slot with identical buy sizes are linked twice; both
    labels must survive, because which signal linked them is the point."""
    sw = [_swap("aaa", slot=1, sol=5.0), _swap("bbb", slot=1, sol=5.0)]
    cc = analyze_coin(MINT, sw, total_supply=1000.0)
    assert len(cc.edges[("aaa", "bbb")]) >= 2


def _edge_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE coin_coordination (token_mint TEXT, phase TEXT, computed_at INT,
        source TEXT, entity_count INT, bundled_supply_pct REAL, bundle_wallet_count INT,
        largest_bundle_size INT, largest_entity_supply_pct REAL, largest_entity_wallet_count INT,
        largest_entity_fresh_ratio REAL, largest_entity_state TEXT,
        PRIMARY KEY (token_mint, phase))""")
    c.execute("""CREATE TABLE coordinated_entities (token_mint TEXT, phase TEXT, entity_id TEXT,
        member_addresses TEXT, wallet_count INT, supply_pct REAL, fresh_ratio REAL, state TEXT,
        edge_sources TEXT, computed_at INT, PRIMARY KEY (token_mint, phase, entity_id))""")
    c.execute("""CREATE TABLE wallet_edges (token_mint TEXT, phase TEXT, wallet_a TEXT,
        wallet_b TEXT, edge_type TEXT, computed_at INT,
        PRIMARY KEY (token_mint, phase, wallet_a, wallet_b, edge_type),
        CHECK (wallet_a < wallet_b))""")
    return c


def test_upsert_persists_every_typed_pair():
    from src.analyzer.coordination import upsert_coordination
    conn = _edge_db()
    sw = [_swap("aaa", slot=1), _swap("bbb", slot=1)]
    cc = analyze_coin(MINT, sw, total_supply=1000.0)
    upsert_coordination(conn, cc, phase="launch")
    rows = conn.execute("SELECT wallet_a, wallet_b, edge_type FROM wallet_edges").fetchall()
    assert rows
    assert {r["edge_type"] for r in rows} == {lbl for lbls in cc.edges.values() for lbl in lbls}


def test_rerun_replaces_rather_than_accumulates():
    """A signal that stops being detected must not leave a stale pair behind."""
    from src.analyzer.coordination import upsert_coordination
    conn = _edge_db()
    cc = analyze_coin(MINT, [_swap("aaa", slot=1), _swap("bbb", slot=1)], total_supply=1000.0)
    upsert_coordination(conn, cc, phase="launch")
    first = conn.execute("SELECT COUNT(*) FROM wallet_edges").fetchone()[0]
    upsert_coordination(conn, cc, phase="launch")
    assert conn.execute("SELECT COUNT(*) FROM wallet_edges").fetchone()[0] == first


def test_phases_are_stored_separately():
    """Launch and post-graduation edges describe different windows and must not
    overwrite each other."""
    from src.analyzer.coordination import upsert_coordination
    conn = _edge_db()
    cc = analyze_coin(MINT, [_swap("aaa", slot=1), _swap("bbb", slot=1)], total_supply=1000.0)
    upsert_coordination(conn, cc, phase="launch")
    upsert_coordination(conn, cc, phase="postgrad")
    phases = {r[0] for r in conn.execute("SELECT DISTINCT phase FROM wallet_edges")}
    assert phases == {"launch", "postgrad"}
