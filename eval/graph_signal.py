"""Does the wallet-edge graph predict anything, once activity is controlled for?

The graph is real: 18% of funder pairs recur across coins, one across 26. This asks
the only question that matters next — whether its SHAPE at the start of a coin's
life separates outcomes.

THE CONTROL IS THE POINT. Two structural claims have already died here on exactly
this: "regular wallets" predicted peak at rho=+0.178 until you noticed plain early
buy count predicted it better (+0.351) and the two correlated at +0.679 — partial
correlation left -0.065 (p=0.273). Anything the graph appears to say is a candidate
for being a restatement of "this coin was busy", which NEGATIVE_RESULTS #17 already
established is not tradable.

So every feature here is tested twice: raw, and within activity bands.

Features are read from wallet_edges over the coordination window
(distribution.COORD_WINDOW_S = 600s, the window where 90% of peaks and 95% of team
exits occur). Outcomes come from coin_trajectory.

    uv run python -m eval.graph_signal
"""

from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

MIN_COINS = 60          # below this no claim is made
MIN_WALLETS = 30        # a graph over a handful of wallets has no shape
SELECTIVE = ("funder", "same_slot")   # see the selectivity table below


def _components(pairs: list[tuple[str, str]]) -> list[set[str]]:
    """Union-find over an edge list; mirrors coordination.assemble_entities."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, set[str]] = defaultdict(set)
    for w in parent:
        groups[find(w)].add(w)
    return [g for g in groups.values() if len(g) >= 2]


def features(conn, mint: str) -> dict | None:
    """Graph shape for one coin, plus the activity control."""
    edges = conn.execute(
        """SELECT wallet_a, wallet_b, edge_type FROM wallet_edges
           WHERE token_mint = ? AND phase = 'postgrad'""", (mint,)).fetchall()
    if not edges:
        return None

    grad = conn.execute(
        "SELECT graduated_at FROM graduation_events WHERE token_mint = ?", (mint,)).fetchone()
    if not grad:
        return None
    g = int(grad[0])

    # THE CONTROL: how busy was the coin in the same window the graph covers.
    n_buys, n_wallets = conn.execute(
        """SELECT COUNT(*), COUNT(DISTINCT wallet_address) FROM post_grad_swaps
           WHERE token_mint = ? AND side = 'buy' AND ts BETWEEN ? AND ? + 600""",
        (mint, g, g)).fetchone()
    if not n_wallets or n_wallets < MIN_WALLETS:
        return None

    sel = [(r["wallet_a"], r["wallet_b"]) for r in edges if r["edge_type"] in SELECTIVE]
    comps = _components(sel)
    sizes = sorted((len(c) for c in comps), reverse=True)
    degree: dict[str, int] = defaultdict(int)
    for a, b in sel:
        degree[a] += 1
        degree[b] += 1
    deg = np.array(list(degree.values()), dtype=float) if degree else np.array([0.0])

    max_pairs = max(n_wallets * (n_wallets - 1) / 2, 1)
    return {
        "mint": mint,
        "n_buys": float(n_buys or 0),
        "n_wallets": float(n_wallets),
        # shape
        "n_components": float(len(comps)),
        "largest_comp_share": (sizes[0] / n_wallets) if sizes else 0.0,
        "clustered_share": (sum(sizes) / n_wallets) if sizes else 0.0,
        "sel_density": len(sel) / max_pairs,
        "max_degree_ratio": float(deg.max() / max(deg.mean(), 1e-9)),
        "funder_edges": float(sum(1 for r in edges if r["edge_type"] == "funder")),
    }


def main() -> int:
    from scipy.stats import rankdata, spearmanr

    from src.common.db import get_connection

    conn = get_connection()
    mints = [r[0] for r in conn.execute(
        "SELECT DISTINCT token_mint FROM wallet_edges WHERE phase = 'postgrad'")]
    rows = []
    for m in mints:
        f = features(conn, m)
        if not f:
            continue
        t = conn.execute(
            """SELECT ct.peak_multiple pk, ct.reached_10x m10 FROM coin_trajectory ct
               JOIN graduation_events ge USING(token_mint)
               JOIN tokens t ON t.mint = ct.token_mint
               WHERE ct.token_mint = ? AND ct.n_price_points >= 30
                 AND COALESCE(ge.is_manufactured,0) = 0
                 AND (SELECT MIN(p.ts) FROM post_grad_swaps p
                      WHERE p.token_mint = ct.token_mint)
                     BETWEEN ge.graduated_at AND ge.graduated_at + 120""", (m,)).fetchone()
        if not t:
            continue
        f["peak"] = float(t["pk"] or 0.0)
        f["moon"] = float(bool(t["m10"]))
        rows.append(f)
    conn.close()

    print(f"coins with a graph and a usable outcome: {len(rows)}")
    if len(rows) < MIN_COINS:
        print(f"SUSPENDED — under {MIN_COINS}. Not 'no signal', no result.")
        return 2

    peak = np.array([r["peak"] for r in rows])
    act = np.array([r["n_buys"] for r in rows])
    shape_keys = ["n_components", "largest_comp_share", "clustered_share",
                  "sel_density", "max_degree_ratio", "funder_edges"]

    print(f"\nactivity control: early buy count vs peak  "
          f"rho={spearmanr(act, peak).statistic:+.3f}\n")
    print(f"{'graph feature':>20} {'raw rho':>9} {'p':>8} {'partial rho':>13} {'p':>8}")
    print("-" * 62)
    rp, ra = rankdata(peak), rankdata(act)
    res_p = rp - np.polyval(np.polyfit(ra, rp, 1), ra)
    for k in shape_keys:
        v = np.array([r[k] for r in rows])
        if np.std(v) == 0:
            continue
        raw = spearmanr(v, peak)
        rv = rankdata(v)
        res_v = rv - np.polyval(np.polyfit(ra, rv, 1), ra)
        par = spearmanr(res_v, res_p)
        print(f"{k:>20} {raw.statistic:>+9.3f} {raw.pvalue:>8.3f} "
              f"{par.statistic:>+13.3f} {par.pvalue:>8.3f}")

    print("\npartial rho removes what early buy count already explains.")
    print("A feature that survives raw but not partial is restating activity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
