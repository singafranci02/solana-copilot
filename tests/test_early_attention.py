from types import SimpleNamespace

from src.analyzer.early_attention import (
    DEFAULT_WINDOW_S, MIN_TRADES, compute_early_attention, to_features,
)

G = 1_000_000


def sw(offset, side, signer, sol=1.0, price=1.0):
    return SimpleNamespace(timestamp=G + offset, side=side, signer=signer,
                           sol_amount=sol, price_usd=price)


def test_thin_tape_returns_none():
    assert compute_early_attention([sw(1, "buy", "a")], G, set()) is None
    assert len(["a"]) < MIN_TRADES


def test_window_excludes_trades_after_cutoff():
    """The leak rule: a trade past the window must not influence the features."""
    inside = [sw(i, "buy", f"w{i}") for i in range(5)]
    leaked = [sw(DEFAULT_WINDOW_S + 60, "buy", "future", sol=999.0)]
    a = compute_early_attention(inside + leaked, G, set())
    assert a.n_trades == 5
    assert a.max_buy_sol == 1.0          # the 999 SOL buy is outside the window


def test_crowd_vs_churn_separated_by_trades_per_wallet():
    crowd = compute_early_attention([sw(i, "buy", f"w{i}") for i in range(20)], G, set())
    churn = compute_early_attention([sw(i, "buy", "w0") for i in range(20)], G, set())
    assert crowd.n_wallets == 20 and crowd.trades_per_wallet == 1.0
    assert churn.n_wallets == 1 and churn.trades_per_wallet == 20.0


def test_accel_reads_crowd_still_arriving_vs_decaying():
    half = DEFAULT_WINDOW_S // 2
    building = [sw(1, "buy", "a"), sw(2, "buy", "b")] + \
               [sw(half + i, "buy", f"n{i}") for i in range(8)]
    dying = [sw(i, "buy", f"e{i}") for i in range(8)] + \
            [sw(half + 1, "buy", "x"), sw(half + 2, "buy", "y")]
    assert compute_early_attention(building, G, set()).accel > 1.0
    assert compute_early_attention(dying, G, set()).accel < 1.0


def test_new_wallet_rate_counts_only_second_half_arrivals():
    half = DEFAULT_WINDOW_S // 2
    swaps = [sw(1, "buy", "a"), sw(2, "buy", "b"),
             sw(half + 1, "buy", "a"),        # returning — not new
             sw(half + 2, "buy", "c")]        # new
    a = compute_early_attention(swaps, G, set())
    assert a.n_wallets == 3
    assert a.new_wallet_rate == 1 / 3


def test_team_sells_excluded_from_retail_flow():
    swaps = [sw(1, "buy", "retail", sol=10.0), sw(2, "sell", "team", sol=8.0),
             sw(3, "buy", "retail2", sol=2.0)]
    a = compute_early_attention(swaps, G, team={"team"})
    assert a.team_sold == 1
    assert a.net_sol == 4.0             # 12 bought - 8 sold, everyone
    assert a.retail_net_sol == 12.0     # the team's exit is not retail demand


def test_price_run_is_peak_over_first_print():
    swaps = [sw(1, "buy", "a", price=2.0), sw(2, "buy", "b", price=8.0),
             sw(3, "sell", "c", price=4.0)]
    assert compute_early_attention(swaps, G, set()).price_run == 4.0


def test_to_features_are_prefixed_floats():
    a = compute_early_attention([sw(i, "buy", f"w{i}") for i in range(5)], G, set())
    f = to_features(a)
    assert f["e5_n_trades"] == 5.0
    assert all(k.startswith("e5_") and isinstance(v, float) for k, v in f.items())


# ── sell structure (risk grading only — never an entry signal) ──────────────────

from src.analyzer.sell_structure import grade_sell_structure


def test_no_team_sell_returns_none():
    swaps = [sw(1, "buy", "retail"), sw(2, "buy", "r2"), sw(3, "sell", "retail")]
    assert grade_sell_structure(swaps, G, {"team"}, 900) is None


def test_lone_seller_who_stopped_is_low_severity():
    swaps = [sw(10, "sell", "t1", sol=1.0), sw(20, "buy", "r", sol=50.0)]
    g = grade_sell_structure(swaps, G, {"t1"}, 900)
    assert g.n_sellers == 1 and not g.still_selling
    assert g.severity == "LOW"


def test_whole_cluster_still_unloading_is_critical():
    swaps = [sw(700, "sell", f"t{i}", sol=5.0) for i in range(4)]
    g = grade_sell_structure(swaps, G, {f"t{i}" for i in range(4)}, 900)
    assert g.n_sellers == 4 and g.still_selling
    assert g.severity == "CRITICAL"


def test_share_of_sell_flow_excludes_retail_sells():
    swaps = [sw(10, "sell", "t1", sol=3.0), sw(11, "sell", "retail", sol=1.0)]
    g = grade_sell_structure(swaps, G, {"t1"}, 900)
    assert g.share_of_sells == 0.75
