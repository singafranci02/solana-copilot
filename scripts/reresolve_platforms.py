"""Background platform re-resolver — drains the 'unverified' bucket, purges Mayhem.

Why this exists: the inline gate at graduation verifies each coin's platform with a
few RPC retries, but under a volume surge the RPC gets hammered and some coins can't
be confirmed in time — they land as 'unverified' (fail-closed: excluded from training,
alerts and the record). This job re-checks those coins on a gentle cadence once the
RPC has recovered, promoting them to 'pump.fun' or catching them as 'mayhem' (which is
then purged from every table). It is the safety net that keeps "can't verify yet" from
silently becoming "trusted".

Runs from launchd every ~10 min. Gentle: small batch, paced, one short DB write.

    uv run python scripts/reresolve_platforms.py [--limit N]
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.graduation_monitor import _is_pump_fun_token

PURGE_TABLES = (
    "graduation_events", "coin_trajectory", "team_clusters", "team_members",
    "post_grad_swaps", "post_grad_behavior", "graduation_feature_snapshot",
    "token_classification", "early_attention", "early_predictions",
    "model_predictions", "team_dump_alerts", "prewarn_alerts", "coin_outcomes",
)


async def _resolve(mints):
    import aiohttp
    from src.ingest.graduation_monitor import resolve_platform
    from src.ingest.solana_tracker import SolanaTrackerClient
    out = {}
    async with SolanaTrackerClient() as st, aiohttp.ClientSession() as rpc_session:
        for m in mints:
            # CHAIN FIRST (free), paid API only when the chain cannot decide. This
            # loop ran every 10 min over up to 60 coins and was the single largest
            # consumer of Solana Tracker credits; the on-chain check resolves most
            # coins on its own, so ST is now the exception rather than the default.
            try:
                p = await resolve_platform(rpc_session, None, mint=m)
                if p is not None:
                    out[m] = p
                    await asyncio.sleep(0.3)
                    continue
            except Exception:
                pass
            # chain undecided -> ask ST once for self-declared-foreign metadata
            try:
                d = await st.get_token_info(m) or {}
                tok = (d.get("token") if isinstance(d.get("token"), dict) else d) or {}
                co = tok.get("createdOn") or ""
                if co and not _is_pump_fun_token(co, m):
                    out[m] = co[:40]                           # self-declared foreign
                    continue
                sig = (tok.get("creation") or {}).get("created_tx")
                if sig:
                    p = await resolve_platform(rpc_session, sig, mint=m)
                    if p is not None:
                        out[m] = p
            except Exception:
                pass                                           # stay unverified, retry
            await asyncio.sleep(0.3)
    return out


def main() -> None:
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 60
    conn = get_connection()
    # newest first: verify fresh coins fast so their alerts aren't suppressed for long
    mints = [r[0] for r in conn.execute(
        """SELECT ge.token_mint FROM graduation_events ge
           JOIN tokens t ON t.mint = ge.token_mint
           WHERE (t.platform IS NULL OR t.platform IN ('unverified', 'pump.fun*'))
             AND ge.graduated_at > strftime('%s','now') - 259200
           ORDER BY ge.graduated_at DESC LIMIT ?""", (limit,))]
    conn.close()
    if not mints:
        print("nothing to re-resolve")
        return

    resolved = asyncio.run(_resolve(mints))

    conn = get_connection()
    # AUTO-RETIRE: a coin still unverified after 6h has had ~36 re-resolver passes;
    # the ones that remain are genuinely unverifiable via our cheap method (their
    # true creation tx is buried under thousands of newer txs, so the mint's reachable
    # earliest signature contains neither the pump nor the Mayhem program). Retire to
    # a terminal 'unresolvable' state so they stop clogging the batch and failing the
    # audit. HARMLESS: fail-closed gating already excludes them from alerts, training
    # and the record — whether they are secretly classic or Mayhem, they never surface.
    retired = conn.execute("""UPDATE tokens SET platform = 'unresolvable'
        WHERE mint IN (SELECT ge.token_mint FROM graduation_events ge
            JOIN tokens t ON t.mint = ge.token_mint
            WHERE (t.platform IS NULL OR t.platform = 'unverified')
              AND ge.graduated_at < strftime('%s','now') - 21600)""").rowcount

    if not resolved:
        conn.commit(); conn.close()
        print(f"re-resolved 0 of {len(mints)} (transient — retry next run); "
              f"retired {retired} coins >6h to unresolvable")
        return

    conn.executemany("UPDATE tokens SET platform = ? WHERE mint = ?",
                     [(p, m) for m, p in resolved.items()])
    conn.commit()

    # Mayhem is KEPT from 2026-08-17 — it is pump.fun's own enhanced mode and is
    # collected as a separate population (platform='mayhem'), never alerted on.
    # Purging still applies to genuinely FOREIGN launchpads (rapidlaunch, bonk.fun,
    # ...), which are a different product and were never in scope.
    foreign = [m for m, p in resolved.items()
               if p != "mayhem" and not str(p).startswith("pump.fun")]
    purged = 0
    for m in foreign:
        for tbl in PURGE_TABLES:
            try:
                purged += conn.execute(f"DELETE FROM {tbl} WHERE token_mint=?", (m,)).rowcount
            except Exception:
                pass
    conn.commit()
    conn.close()

    n_pump = sum(1 for p in resolved.values() if p == "pump.fun")
    n_may = sum(1 for p in resolved.values() if p == "mayhem")
    print(f"re-resolved {len(resolved)}/{len(mints)}: {n_pump} classic, "
          f"{n_may} mayhem kept, {len(foreign)} foreign purged ({purged} rows)")
    if foreign:                       # rebuild aggregates only when something was removed
        import subprocess
        subprocess.run([sys.executable, "scripts/rebuild_funder_lineage.py"],
                       cwd=Path(__file__).parent.parent, capture_output=True)


if __name__ == "__main__":
    main()
