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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

POLL_INTERVAL_S = 300          # see the table above; 5 min buys ~92% reach
ANCHOR_WINDOW_S = 120          # must match eval._common.MAX_ANCHOR_LAG_S
MAX_RECOVERY_AGE_S = 3600      # older than this, the tape is out of reach anyway
PUMP_MARKETS = {"pumpfun", "pumpfun-amm"}


def _true_graduation_ts(pools: list[dict]) -> int | None:
    """The AMM pool is created AT migration, so its createdAt is the true zero."""
    for p in pools or []:
        if p.get("market") in PUMP_MARKETS and p.get("createdAt"):
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

    swaps = await st.get_token_trades(mint, since_ts=grad_ts)
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
                bc_top_holders_json, recovered)
           VALUES (?,?,?,2,'[]',1)""",
        (mint, grad_ts, int(time.time()) - grad_ts))
    upsert_swaps(conn, mint, swaps, sniper_wallets=set(), team_wallets=set())
    conn.commit()
    return "recovered"


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
