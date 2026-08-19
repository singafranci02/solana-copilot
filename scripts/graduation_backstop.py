"""Recover graduations the WebSocket never delivered.

Measured 2026-08-18 against an independent graduated feed: 27% of graduations
never arrive on the PumpPortal socket at all. They are not skipped or misjudged —
they are simply never seen, so nothing downstream can notice they are missing.

WHY THIS IS RECOVERABLE, and the one thing that makes it safe:

The label anchor is not "when did we detect it". It is the coin's true graduation
moment, and that is recoverable after the fact from the AMM pool's creation
timestamp. Validated against 25 coins we DID catch live: pool createdAt sits a
median 34s BEFORE our own socket-recorded graduated_at (p10 -45s, p90 -21s, all
within +/-120s) — i.e. the pool timestamp is the truer zero point, and our live
path is the one running slightly late.

From that anchor the first trade lands a median +34s later (p90 +52s), so 97% of
coins satisfy the 120s anchor gate REGARDLESS of when we notice them.

    Recovery therefore anchors on pool createdAt. It must never anchor on poll
    time. Anchoring on detection would pass the gate while measuring from a
    post-dump price — which is precisely how the August outage manufactured a
    25.1% survival rate against a true 5.2%.

WHAT ACTUALLY BINDS: the trade API pages newest-first, ~1500 trades within budget,
so the walk must span from now back to graduation. That is a function of trade
VOLUME, not elapsed time, which makes the poll interval the real constraint:

    poll delay   trades accumulated (p50)   within reach
        2 min            245                    97%
        5 min            544                    92%
       10 min            895                    77%
       60 min           1531                    47%

Hence POLL_INTERVAL_S = 300. Coins whose tape cannot be walked back to the anchor
are left alone rather than recorded with a bad zero point — fail closed, as
everywhere else in this pipeline.

KNOWN LIMITATION, recorded rather than hidden: the structural snapshot (team
cluster, holder concentration) is read from CURRENT chain state, so for a coin
recovered N minutes late it is N minutes stale. The membership gate still requires
bonding-curve accumulation, which bounds the damage — a pure post-graduation buyer
cannot become "team" — but recovered coins are flagged so they can be included or
excluded from any population deliberately.

    uv run python scripts/graduation_backstop.py [--dry-run] [--limit N]
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 300          # see the table above; 5 min buys ~92% reach
ANCHOR_WINDOW_S = 120          # must match eval._common.MAX_ANCHOR_LAG_S
MAX_RECOVERY_AGE_S = 3600      # older than this, the tape is out of reach anyway
PUMP_MARKETS = {"pumpfun", "pumpfun-amm"}

# Pages of ~250 trades walked back from now to reach the graduation anchor. The
# global default is 6 (an API-budget cap for routine fetches) and it was silently
# rejecting the most valuable coins: a busy launch prints thousands of trades in
# its first minutes, so 1,500 trades only reaches ~350s back and the anchor check
# fails. Observed live on 98Jxwygc: rejected at +349s with 6 pages, +245s with 12,
# and +67s with 25 — the same coin, PASSING the 120s gate once paged deep enough.
#
# This costs nothing on quiet coins: the DESC walk early-stops as soon as a page
# predates since_ts, so the cap is only reached by coins that genuinely need it —
# which are exactly the ones worth recovering.
ANCHOR_SEEK_PAGES = 30


# The AMM pool is created AT migration; the curve pool is created at LAUNCH. Only
# the former is the graduation moment.
AMM_MARKET = "pumpfun-amm"


def _true_graduation_ts(pools: list[dict]) -> int | None:
    """The AMM pool's createdAt — the migration moment, and the label anchor.

    This must select the AMM pool EXPLICITLY. It previously returned the first pool
    whose market was in PUMP_MARKETS, which includes "pumpfun" — the bonding-curve
    pool, whose createdAt is the LAUNCH time. It was correct only because the feed
    happens to list the AMM first (40/40 checked). If that order ever flips, a coin
    is anchored to its launch instead of its migration, the 120s gate then passes
    trivially because the first trade lands seconds after launch, and every label on
    that coin is measured from the wrong zero — the exact corruption that produced a
    25.1% survival rate against a true 5.2% in August.

    Harmless today on instant graduations (curve and AMM within ~1s) and severe on
    long ones: a 31-hour bonding curve would be anchored 31 hours early.
    """
    for p in pools or []:
        if p.get("market") == AMM_MARKET and p.get("createdAt"):
            return int(p["createdAt"] / 1000)
    return None


async def find_missed(st, conn, limit: int) -> list[tuple[str, int]]:
    """[(mint, true_graduated_at)] for graduations we never saw."""
    feed = await st._get("/tokens/multi/graduated")
    now = int(time.time())
    out = []
    for entry in feed or []:
        tok = entry.get("token") or {}
        mint = tok.get("mint") or entry.get("mint")
        if not mint:
            continue
        seen = conn.execute(
            """SELECT 1 FROM graduation_events WHERE token_mint=?
               UNION ALL SELECT 1 FROM skipped_graduations WHERE token_mint=?""",
            (mint, mint)).fetchone()
        if seen:
            continue
        grad = _true_graduation_ts(entry.get("pools") or [])
        if grad is None:
            continue                      # not a pump.fun AMM pool — out of scope
        if now - grad > MAX_RECOVERY_AGE_S:
            continue                      # too old for the tape walk to reach
        out.append((mint, grad))
        if len(out) >= limit:
            break
    return out


async def recover(mint: str, grad_ts: int, st, conn) -> str:
    """Returns a one-word outcome for the run summary."""
    from src.analyzer.post_grad_swaps import upsert_swaps

    swaps = await st.get_token_trades(mint, since_ts=grad_ts,
                                      max_pages=ANCHOR_SEEK_PAGES)
    if not swaps:
        return "no-tape"

    earliest = min(s.timestamp for s in swaps)
    lag = earliest - grad_ts
    # THE GATE. If the walk could not reach the anchor, the coin's zero point is
    # unverified and every label built on it would be measured from the wrong
    # start. Leave it alone rather than record a plausible-looking lie.
    if not 0 <= lag <= ANCHOR_WINDOW_S:
        return f"anchor-miss({lag:+.0f}s)"

    # tokens first — graduation_events carries a foreign key onto it. Platform
    # starts 'unverified' so the coin is invisible to alerts and to every training
    # population until the re-resolver confirms it on-chain; recovery must not be a
    # side door around the platform gate.
    # launchpad and created_at are NOT NULL without defaults, so a partial
    # INSERT OR IGNORE is silently discarded and the foreign key below then fails.
    conn.execute(
        """INSERT OR IGNORE INTO tokens (mint, launchpad, created_at, platform)
           VALUES (?, 'pump.fun', ?, 'unverified')""",
        (mint, grad_ts))
    conn.execute(
        """INSERT OR IGNORE INTO graduation_events
               (token_mint, graduated_at, detection_lag_seconds, pipeline_version,
                bc_top_holders_json, recovered, detection_source)
           VALUES (?,?,?,2,'[]',1,'poll')""",
        (mint, grad_ts, int(time.time()) - grad_ts))
    upsert_swaps(conn, mint, swaps, sniper_wallets=set(), team_wallets=set())

    # RUN THE LABELER. Storing tape is not ingestion: without this the coin has a
    # graduation row and a tape and contributes NOTHING, because every population
    # joins through coin_trajectory. Measured before this was added: 198 recovered
    # coins, 198 with tape, 0 with a trajectory — the backstop was producing rows
    # the labeler never saw.
    #
    # LIMIT, stated rather than hidden: trajectory_from_db derives team-exit timing
    # from post_grad_swaps.is_team, and a recovered coin has no team cluster because
    # the cluster is a graduation-MOMENT holder read that cannot be reconstructed
    # afterwards. So these coins yield price-trajectory labels (collapse, peak,
    # n_price_points) and a NULL team exit. They count toward base rates and the
    # Mayhem comparison; they cannot contribute team-behaviour labels.
    # STRUCTURAL ANALYSIS through the SAME path the live monitor uses. Without it a
    # recovered coin had a tape and a trajectory but no holders, no bonding-curve
    # buyers and no team cluster — measured 0 of 207 — so its team-exit label was
    # structurally NULL and it could never contribute the one kind of label this
    # system predicts. analyse_graduation is invoked directly rather than
    # reimplemented, so recovery cannot drift from live behaviour.
    #
    # Safe on alerts: the coin is still platform='unverified' here, and both
    # coin-level alerts gate on platform == 'pump.fun'.
    try:
        await _structural(conn, mint, grad_ts, st)
    except Exception as exc:
        logger.debug("structural analysis failed for %s: %s", mint[:8], exc)

    # LABEL LAST. time_to_team_exit_s is the first sell where is_team=1, so the
    # trajectory must be computed AFTER the team cluster exists and its flags are
    # stamped — otherwise the exit label is NULL for exactly the reason this
    # structural pass was added to fix.
    from src.analyzer.distribution import _apply_tape_flags
    from src.analyzer.trajectory import trajectory_from_db, upsert_trajectory
    try:
        row = conn.execute(
            "SELECT member_addresses FROM team_clusters WHERE token_mint = ? LIMIT 1",
            (mint,)).fetchone()
        members = set(json.loads(row["member_addresses"] or "[]")) if row else set()
        _apply_tape_flags(conn, mint, members, set(), set())
        upsert_trajectory(conn, trajectory_from_db(conn, mint, grad_ts))
        conn.commit()
    except Exception as exc:
        logger.debug("labelling failed for %s: %s", mint[:8], exc)
    return "recovered"


async def _structural(conn, mint: str, grad_ts: int, st) -> None:
    """Holders + BC reconstruction + team clustering, via the live code path."""
    import aiohttp

    from src.common.models import GraduationEvent
    from src.ingest.graduation_monitor import (
        _BC_RECONSTRUCT_TOP_N, _parse_bc_holders, _reconstruct_bc, analyse_graduation,
    )
    from src.ingest.rpc_holders import get_token_holders_resilient, get_token_supply_rpc

    from src.analyzer.structural_accounts import structural_set
    from src.common.cex_wallets import get_all_cex_addresses

    async with aiohttp.ClientSession() as s:
        accounts, _src = await get_token_holders_resilient(s, mint, st)
        supply = await get_token_supply_rpc(s, mint)
    if not accounts:
        return

    # PARITY WITH THE LIVE PATH. Recovery previously passed structural=frozenset()
    # and did not pre-filter accounts, so the AMM pool, the bonding curve and program
    # vaults could become token_buyers and then team members on recovered coins —
    # while the same coin captured live would have excluded them. token_info is the
    # source of the per-mint pool accounts, exactly as graduation_monitor does it.
    try:
        info = await st.get_token_info(mint)
    except Exception:
        info = None
    structural = structural_set(info, get_all_cex_addresses(conn))
    accounts = [a for a in accounts if a.get("address") not in structural]
    if not accounts:
        return

    # Holder state is read NOW, so it is stale by the poll delay (~5 min). The
    # membership gate still requires bonding-curve accumulation or an unexplained
    # large position, which bounds what a post-graduation buyer can become.
    bc_top_holders = _parse_bc_holders(accounts, supply)
    if not bc_top_holders:
        return

    bc_swaps = await _reconstruct_bc(
        st, mint, bc_top_holders, 0, grad_ts, conn, structural=structural)

    buyers = [r for r in conn.execute(
        """SELECT wallet_address, bought_at, sol_amount, tokens_received
           FROM token_buyers WHERE token_mint = ?""", (mint,))]
    from src.common.models import TokenBuyer
    buyer_objs = [TokenBuyer(token_mint=mint, wallet_address=b[0], bought_at=b[1],
                             sol_amount=b[2], tokens_received=b[3]) for b in buyers]

    event = GraduationEvent(token_mint=mint, graduated_at=grad_ts,
                            bc_top_holders=bc_top_holders)
    await analyse_graduation(event, buyer_objs, conn, symbol=mint[:6])


async def main() -> None:
    from src.ingest.solana_tracker import SolanaTrackerClient

    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 40

    conn = get_connection()
    async with SolanaTrackerClient() as st:
        missed = await find_missed(st, conn, limit)
        print(f"{len(missed)} graduations in the feed that we never saw")
        if dry:
            for m, g in missed[:10]:
                print(f"  {m[:12]}  graduated {(time.time()-g)/60:.1f} min ago")
            conn.close()
            return

        tally: dict[str, int] = {}
        for mint, grad in missed:
            outcome = await recover(mint, grad, st, conn)
            key = outcome.split("(")[0]
            tally[key] = tally.get(key, 0) + 1
            print(f"  {mint[:12]}  {(time.time()-grad)/60:5.1f}m old  {outcome}")
    conn.close()
    print(f"\n{tally}")


if __name__ == "__main__":
    asyncio.run(main())
