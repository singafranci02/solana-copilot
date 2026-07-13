"""Platform sweep v2: verify EVERY graduated token on-chain, purge non-pump.fun.

Two facts drive the design (both verified live):
  1. Foreign platforms that DECLARE themselves (rapidlaunch, bags, ...) are caught
     by metadata createdOn.
  2. MAYHEM does not declare itself — it creates tokens by CPI THROUGH pump.fun
     (createdOn says pump.fun, venue pump-amm, mint ends 'pump'). Only the creation
     TRANSACTION betrays it: Mayhem's program MAyhSmz... is present in it.

So each mint needs: token-info (createdOn + created_tx, Solana Tracker) and one
getTransaction (free RPC). Resumable: progress commits every 100 mints and already-
classified mints are skipped, so it can be re-run after any interruption.

    uv run python scripts/sweep_platform.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.graduation_monitor import _is_pump_fun_token, _platform_from_tx

PURGE_TABLES = (
    "graduation_events", "coin_trajectory", "team_clusters", "team_members",
    "post_grad_swaps", "post_grad_behavior", "graduation_feature_snapshot",
    "token_classification", "early_attention", "early_predictions",
    "model_predictions", "team_dump_alerts", "prewarn_alerts", "coin_outcomes",
)


async def classify(mints, conn):
    from src.ingest.rpc import RpcClient
    from src.ingest.solana_tracker import SolanaTrackerClient
    t0 = time.time()
    async with SolanaTrackerClient() as st, RpcClient() as rpc:
        for i, m in enumerate(mints):
            platform = None
            created_on = None
            try:
                d = await st.get_token_info(m) or {}
                tok = d.get("token") if isinstance(d.get("token"), dict) else d
                tok = tok or {}
                created_on = tok.get("createdOn") or ""
                if not _is_pump_fun_token(created_on or None, m):
                    platform = (created_on or "unknown")[:40]     # self-declared foreign
                else:
                    tx_sig = (tok.get("creation") or {}).get("created_tx")
                    if tx_sig:
                        tx = await rpc._call("getTransaction", [tx_sig,
                            {"maxSupportedTransactionVersion": 0, "encoding": "json"}])
                        platform = _platform_from_tx(tx)
                    if platform is None:
                        platform = "pump.fun*"    # metadata says pump, tx unresolvable
            except Exception:
                platform = None                    # fully unresolved — retry next run
            if platform is not None:
                conn.execute(
                    "UPDATE tokens SET platform = ?, created_on = ? WHERE mint = ?",
                    (platform, created_on, m))
            if (i + 1) % 100 == 0:
                conn.commit()
                rate = (i + 1) / (time.time() - t0)
                print(f"  {i+1}/{len(mints)}  ({rate:.1f}/s, "
                      f"eta {(len(mints)-i-1)/rate/60:.0f}m)", flush=True)
    conn.commit()


def main() -> None:
    conn = get_connection()
    todo = [r[0] for r in conn.execute(
        """SELECT ge.token_mint FROM graduation_events ge
           JOIN tokens t ON t.mint = ge.token_mint
           WHERE t.platform IS NULL
           ORDER BY ge.graduated_at DESC""")]
    print(f"mints needing platform classification: {len(todo)}", flush=True)
    asyncio.run(classify(todo, conn))

    dist = list(conn.execute(
        """SELECT t.platform, COUNT(*) FROM graduation_events ge
           JOIN tokens t ON t.mint = ge.token_mint GROUP BY 1 ORDER BY 2 DESC"""))
    print("\nplatform distribution of all graduations:")
    for lp, n in dist:
        print(f"  {lp or '(unresolved)'}: {n}")

    bad = [r[0] for r in conn.execute(
        """SELECT ge.token_mint FROM graduation_events ge JOIN tokens t ON t.mint=ge.token_mint
           WHERE t.platform IS NOT NULL
             AND t.platform NOT IN ('pump.fun','pump.fun*')""")]
    print(f"\npurging {len(bad)} non-pump.fun graduations")
    for tbl in PURGE_TABLES:
        n = 0
        for m in bad:
            try:
                n += conn.execute(f"DELETE FROM {tbl} WHERE token_mint=?", (m,)).rowcount
            except Exception:
                pass
        if n:
            print(f"  {tbl}: -{n}")
    conn.commit()
    conn.close()

    import subprocess
    subprocess.run([sys.executable, "scripts/rebuild_funder_lineage.py"],
                   cwd=Path(__file__).parent.parent)
    subprocess.run([sys.executable, "scripts/track_record.py"],
                   cwd=Path(__file__).parent.parent)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()
