"""MANUFACTURED graduations — curves bought to force migration, not organic launches.

The pattern (owner's framing: "one team just buys it all to get it migrated and it's
one big vertical line"): a single entity or bundle fills the bonding curve in minutes,
graduation is the liquidity event, and there is no organic market at any point.
Measured on classic pump.fun coins: 32% graduate in <10 minutes, the top decile has
5 wallets holding 86%+ of curve buying, and flagged coins insta-collapse (<=5 min)
at 40% vs 23% for clean ones.

POLICY (differs from the Mayhem purge deliberately):
  - these ARE classic pump.fun coins -> they stay in the DB and the live pipeline
    still analyses them: recognising one and saying SKIP is product value
  - they are EXCLUDED from every model-training and label population: their tape is
    one entity's puppet show, not price discovery, and it teaches the model lies
  - their alerts carry an explicit annotation instead of being suppressed

Detection is >=2 independent flags — any single metric misfires (a hyped organic
launch can also graduate fast; a whale can hold 50% of an organic curve).
"""

from __future__ import annotations

LIGHTNING_MINUTES = 10        # creation -> graduation faster than this
FEW_BUYERS = 25               # distinct BC buyers below this
TOP5_SHARE = 0.60             # top-5 buyers took >= this share of the curve
TEAM_SUPPLY = 50.0            # team holds >= this % at graduation
SLOT_BUNDLE = 8               # >= this many buys landed in one slot
MIN_FLAGS = 2


def manufactured_flags(
    bc_duration_s: float | None,
    n_bc_buyers: int | None,
    top5_buyer_share: float | None,
    team_supply_pct: float | None,
    max_same_slot_group: int | None,
) -> list[str]:
    """The independent red flags present for one graduation. Pure."""
    fl = []
    if bc_duration_s is not None and 0 < bc_duration_s < LIGHTNING_MINUTES * 60:
        fl.append("lightning_curve")
    if n_bc_buyers is not None and 0 < n_bc_buyers < FEW_BUYERS:
        fl.append("few_buyers")
    if top5_buyer_share is not None and top5_buyer_share >= TOP5_SHARE:
        fl.append("bundled_buying")
    if team_supply_pct is not None and min(team_supply_pct, 100.0) >= TEAM_SUPPLY:
        fl.append("team_majority")
    if max_same_slot_group is not None and max_same_slot_group >= SLOT_BUNDLE:
        fl.append("slot_bundle")
    return fl


def is_manufactured(flags: list[str]) -> bool:
    return len(flags) >= MIN_FLAGS


def detect_and_stamp(conn, token_mint: str) -> list[str]:
    """Compute flags from already-stored rows (zero API calls) and stamp
    graduation_events. Same code path serves live analysis and backfills."""
    import json
    row = conn.execute(
        """SELECT ge.graduated_at g, t.created_at c, t.created_at_source src,
                  tc.supply_pct_at_graduation sup,
                  bf.n_buyers ub, bf.top5_buyer_share t5, bf.max_same_slot_group msg
           FROM graduation_events ge
           LEFT JOIN tokens t ON t.mint = ge.token_mint
           LEFT JOIN team_clusters tc ON tc.token_mint = ge.token_mint
           LEFT JOIN bc_flow_features bf ON bf.token_mint = ge.token_mint
           WHERE ge.token_mint = ?""", (token_mint,)).fetchone()
    if not row:
        return []
    dur = None
    if row["c"] and row["src"] != "fallback_now" and row["g"] > row["c"]:
        dur = float(row["g"] - row["c"])
    fl = manufactured_flags(dur, row["ub"], row["t5"], row["sup"], row["msg"])
    conn.execute(
        "UPDATE graduation_events SET is_manufactured=?, manufactured_flags=? "
        "WHERE token_mint=?",
        (int(is_manufactured(fl)), json.dumps(fl), token_mint))
    return fl
