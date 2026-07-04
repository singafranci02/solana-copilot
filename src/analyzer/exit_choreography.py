"""Team exit choreography (Phase D) — the behavioral-economics core.

A coordinated team exits a coin in a recognizable SEQUENCE: a leader wallet
sells first, others follow within a tight window, positions unwind in order.
Organic holders exit independently and randomly. Tracking WHO sells first, in
what ORDER, and how TIGHTLY spread the exits are — per coin and rolled up per
funder — is the strongest behavioral discriminator between an insider ring and
a genuine holder base.

Pure computation over the team's post-graduation swap tape (already fetched by
distribution._do_check). Per-member rows → team_member_behavior; funder rollup
→ team_fingerprints choreography columns.
"""

import time
from dataclasses import dataclass, field


@dataclass
class MemberExit:
    wallet: str
    exit_order: int | None            # 1 = first team seller; None = never sold
    first_sell_offset_s: float | None
    sold_pct: float | None            # of graduation-time position
    is_first_seller: bool
    participated_coordinated_sell: bool


@dataclass
class ExitChoreography:
    members: list[MemberExit] = field(default_factory=list)
    leader_wallet: str | None = None      # first seller
    exit_spread_s: float | None = None    # last member's first-sell − first member's
    n_sellers: int = 0


def compute_exit_choreography(
    team_swaps: list,                 # Swap list, team members only
    grad_positions: dict[str, float],  # wallet → tokens held at graduation
    graduated_at: int,
    team_members: set[str],
    coordinated_windows: list[set[str]] | None = None,
) -> ExitChoreography:
    """Per-member exit behavior + team-level choreography (pure)."""
    coordinated_windows = coordinated_windows or []
    coordinated_wallets: set[str] = set()
    for w in coordinated_windows:
        coordinated_wallets |= w

    # first sell time + sold tokens per member
    first_sell: dict[str, int] = {}
    sold_tokens: dict[str, float] = {}
    for s in team_swaps:
        if s.side != "sell" or s.signer not in team_members:
            continue
        sold_tokens[s.signer] = sold_tokens.get(s.signer, 0.0) + s.token_amount
        ts = int(s.timestamp)
        if s.signer not in first_sell or ts < first_sell[s.signer]:
            first_sell[s.signer] = ts

    # exit order by first-sell time
    ordered = sorted(first_sell.items(), key=lambda kv: kv[1])
    order_of = {w: i + 1 for i, (w, _) in enumerate(ordered)}
    leader = ordered[0][0] if ordered else None

    members: list[MemberExit] = []
    for w in sorted(team_members):
        fs = first_sell.get(w)
        pos = grad_positions.get(w)
        sold_pct = None
        if pos and pos > 0:
            sold_pct = round(min(sold_tokens.get(w, 0.0) / pos, 1.0) * 100, 2)
        members.append(MemberExit(
            wallet=w,
            exit_order=order_of.get(w),
            first_sell_offset_s=float(fs - graduated_at) if fs is not None else None,
            sold_pct=sold_pct,
            is_first_seller=(w == leader),
            participated_coordinated_sell=w in coordinated_wallets,
        ))

    spread = None
    if len(ordered) >= 2:
        spread = float(ordered[-1][1] - ordered[0][1])

    return ExitChoreography(
        members=members, leader_wallet=leader, exit_spread_s=spread,
        n_sellers=len(ordered),
    )


def upsert_team_member_behavior(
    conn, token_mint: str, choreo: ExitChoreography, offset_h: int,
) -> None:
    """Upsert per-member exit rows; the matching sold_pct_{offset}h column only."""
    if not choreo.members:
        return
    col = {1: "sold_pct_1h", 4: "sold_pct_4h", 24: "sold_pct_24h"}.get(offset_h)
    now = int(time.time())
    for m in choreo.members:
        conn.execute(
            f"""INSERT INTO team_member_behavior
                   (token_mint, wallet, exit_order, first_sell_offset_s,
                    {col}, is_first_seller, participated_coordinated_sell, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_mint, wallet) DO UPDATE SET
                   exit_order=excluded.exit_order,
                   first_sell_offset_s=excluded.first_sell_offset_s,
                   {col}=excluded.{col},
                   is_first_seller=excluded.is_first_seller,
                   participated_coordinated_sell=excluded.participated_coordinated_sell,
                   updated_at=excluded.updated_at""",
            (
                token_mint, m.wallet, m.exit_order, m.first_sell_offset_s,
                m.sold_pct, int(m.is_first_seller),
                int(m.participated_coordinated_sell), now,
            ),
        )
    conn.commit()


def update_funder_choreography(
    conn, funding_source: str | None, choreo: ExitChoreography,
) -> None:
    """Roll a coin's choreography into the funder's team_fingerprints row:
    running mean of exit_spread, and leader-consistency (does the same wallet
    lead exits across this funder's launches?). Gate n>=8 applied at read time."""
    if not funding_source or funding_source == "cex" or choreo.leader_wallet is None:
        return
    row = conn.execute(
        """SELECT avg_exit_spread_s, leader_wallet, leader_consistency,
                  choreography_sample_count
           FROM team_fingerprints WHERE funding_source = ?""",
        (funding_source,),
    ).fetchone()
    spread = choreo.exit_spread_s if choreo.exit_spread_s is not None else 0.0

    if row is None or row["choreography_sample_count"] == 0:
        # First choreography observation for this funder (row may exist from the
        # structural writer). Ensure a row, then set initial choreography state.
        import uuid
        conn.execute(
            """INSERT INTO team_fingerprints
                   (fingerprint_id, funding_source, avg_exit_spread_s, leader_wallet,
                    leader_consistency, choreography_sample_count)
               VALUES (?, ?, ?, ?, 1.0, 1)
               ON CONFLICT(funding_source) DO UPDATE SET
                   avg_exit_spread_s=excluded.avg_exit_spread_s,
                   leader_wallet=excluded.leader_wallet,
                   leader_consistency=excluded.leader_consistency,
                   choreography_sample_count=1""",
            (str(uuid.uuid4()), funding_source, spread, choreo.leader_wallet),
        )
    else:
        n = int(row["choreography_sample_count"])
        new_n = n + 1
        new_spread = round((float(row["avg_exit_spread_s"] or 0) * n + spread) / new_n, 2)
        # leader consistency: fraction of launches led by the modal leader. Simple
        # online estimate — nudge up when the leader repeats, down otherwise.
        same = row["leader_wallet"] == choreo.leader_wallet
        prev_consistency = float(row["leader_consistency"] or 0)
        new_consistency = round((prev_consistency * n + (1.0 if same else 0.0)) / new_n, 4)
        leader = row["leader_wallet"] if not same and new_consistency >= 0.5 else choreo.leader_wallet
        conn.execute(
            """UPDATE team_fingerprints SET
                   avg_exit_spread_s=?, leader_wallet=?, leader_consistency=?,
                   choreography_sample_count=?
               WHERE funding_source=?""",
            (new_spread, leader, new_consistency, new_n, funding_source),
        )
    conn.commit()
