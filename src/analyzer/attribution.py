"""Is "the team never sold" a measurement, or a blind spot?

The team-exit label has been binary — an exit was seen, or the team held. Measured
2026-08-13, that binary is wrong for a specific, biased slice: among low-supply
teams, 45% show no exit at all, against 4% for teams holding >=35%. Those coins
have a median of 3 detected members and ZERO team trades on the tape, so "held" was
frequently recording that we saw nothing, not that nothing happened. The bias runs
in the flattering direction, which is the dangerous one for a reputation score.

Splitting those 34 coins by whether ANY bonding-curve buyer outside the gated team
sold post-graduation:

    68% (23)  nobody in the funding graph sold      -> HELD is defensible
    32% (11)  an ungated BC buyer DID sell          -> attribution is UNCERTAIN

The instinct is to widen the membership gate to catch that 32%. Do not. Recovering
a median of 6 missed sellers means admitting a median of 458 ungated BC buyers as
candidates — ~1.3% precision, which re-creates the exact failure the gate exists to
prevent (86 "team" wallets per coin at 9.8% insider precision, versus 26.7% for
corroborated buyer-and-holder members). Widening a shared gate would also degrade
the high-supply population, which is currently fine.

So this does not touch passes_member_gate. It adds the third state the schema was
missing and excludes it from labels — the same move as the anchor gate, which is
what restored rug ROC after the outage: when a measurement cannot be trusted,
withhold it rather than guess.
"""

from __future__ import annotations

OBSERVED = "observed"       # a gated team member sold; the exit time is measured
HELD = "held"               # no team sell, and no ungated funding-graph seller either
UNCERTAIN = "uncertain"     # no team sell, but an ungated BC buyer sold — we may be blind

# Above this supply the blind spot effectively disappears (4% no-exit vs 45%), so the
# expensive ungated check is skipped and absence of a sell is taken at face value.
SUPPLY_TRUST_PCT = 10.0


def classify_attribution(conn, token_mint: str) -> tuple[str, int]:
    """Returns (state, n_ungated_sellers). Cheap: two indexed reads at most."""
    row = conn.execute(
        """SELECT ct.time_to_team_exit_s tx, tc.member_addresses ma,
                  COALESCE(tc.supply_pct_at_graduation, 0) sup
           FROM coin_trajectory ct
           LEFT JOIN team_clusters tc ON tc.token_mint = ct.token_mint
           WHERE ct.token_mint = ?""", (token_mint,)).fetchone()
    if row is None:
        return UNCERTAIN, 0
    if row["tx"] is not None:
        return OBSERVED, 0
    if float(row["sup"] or 0) >= SUPPLY_TRUST_PCT:
        return HELD, 0

    import json
    members = set(json.loads(row["ma"] or "[]"))
    bc = {r[0] for r in conn.execute(
        "SELECT DISTINCT wallet_address FROM token_buyers WHERE token_mint = ?",
        (token_mint,))}
    candidates = bc - members
    if not candidates:
        return HELD, 0

    marks = ",".join("?" * len(candidates))
    n = conn.execute(
        f"""SELECT COUNT(DISTINCT wallet_address) FROM post_grad_swaps
            WHERE token_mint = ? AND side = 'sell'
              AND wallet_address IN ({marks})""",
        (token_mint, *candidates)).fetchone()[0]
    return (UNCERTAIN, n) if n else (HELD, 0)


def upsert_attribution(conn, token_mint: str) -> str:
    state, n = classify_attribution(conn, token_mint)
    conn.execute(
        """INSERT OR REPLACE INTO coin_attribution
               (token_mint, state, n_ungated_sellers, computed_at)
           VALUES (?,?,?, strftime('%s','now'))""", (token_mint, state, n))
    return state
