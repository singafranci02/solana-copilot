"""Tests for src/analyzer/holder_snapshot.py — pure functions only (offline)."""

from src.analyzer.holder_snapshot import compute_holder_snapshot, detect_new_entrants


def _acct(addr, ui):
    return {"address": addr, "uiAmount": ui}


# ── compute_holder_snapshot ─────────────────────────────────────────────────────

def test_holder_count_and_top10_pct():
    accounts = [_acct(f"W{i}", 100) for i in range(5)]  # 5 holders, 100 each = 500
    m = compute_holder_snapshot(accounts, grad_holder_set=set(), total_supply=1000)
    assert m.holder_count == 5
    assert m.top10_pct == 50.0  # 500 / 1000


def test_new_and_churned_counts():
    accounts = [_acct("A", 10), _acct("B", 10), _acct("C", 10)]  # current: A,B,C
    grad = {"A", "B", "X", "Y"}  # X,Y churned; C is new
    m = compute_holder_snapshot(accounts, grad_holder_set=grad, total_supply=100)
    assert m.new_holder_count == 1   # C
    assert m.churned_holder_count == 2  # X, Y


def test_empty_supply_guard():
    accounts = [_acct("A", 0)]
    m = compute_holder_snapshot(accounts, grad_holder_set=set(), total_supply=0)
    assert m.top10_pct == 0.0


def test_top10_caps_at_ten_largest():
    accounts = [_acct(f"W{i}", 10) for i in range(15)]  # 15 holders × 10 = 150
    m = compute_holder_snapshot(accounts, grad_holder_set=set(), total_supply=150)
    # only top 10 counted → 100 / 150
    assert m.top10_pct == round(100 / 150 * 100, 2)


# ── detect_new_entrants ─────────────────────────────────────────────────────────

def test_new_entrants_excludes_grad_holders():
    swap_wallets = {"A", "B", "C"}
    grad = {"A"}
    entrants = detect_new_entrants(swap_wallets, grad, smart_money_set=set())
    addrs = {e.wallet for e in entrants}
    assert addrs == {"B", "C"}


def test_new_entrant_smart_money_flag():
    entrants = detect_new_entrants({"B", "C"}, grad_holder_set=set(), smart_money_set={"B"})
    by_wallet = {e.wallet: e.is_smart_money for e in entrants}
    assert by_wallet["B"] is True
    assert by_wallet["C"] is False


def test_no_entrants_when_all_grad_holders():
    entrants = detect_new_entrants({"A", "B"}, grad_holder_set={"A", "B"}, smart_money_set=set())
    assert entrants == []
