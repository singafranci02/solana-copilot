"""One-shot platform sweep: verify EVERY graduated token's creation platform.

Fetches token-info createdOn for every mint in graduation_events (persisted to
tokens.created_on so the nightly audit never needs API calls), then purges every
coin that is not pump.fun from all graduation tables and rebuilds the aggregates.

Why: the venue label was not enough — Mayhem (and others) migrate to PumpSwap and
even share the 'pump' mint suffix. createdOn is the discriminator (what GMGN's
launchpad filter uses). Missing metadata falls back to the mint suffix.

    uv run python scripts/sweep_platform.py
"""

import asyncio
import json
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


async def fetch_all(mints):
    from src.ingest.solana_tracker import SolanaTrackerClient
    got = {}
    t0 = time.time()
    async with SolanaTrackerClient() as st:
        for i, m in enumerate(mints):
            try:
                d = await st.get_token_info(m) or {}
                tok = d.get("token") if isinstance(d.get("token"), dict) else d
                got[m] = (tok or {}).get("createdOn") or ""
            except Exception:
                got[m] = None                    # fetch failed — retryable, not purged
            if (i + 1) % 500 == 0:
                rate = (i + 1) / (time.time() - t0)
                print(f"  {i+1}/{len(mints)}  ({rate:.1f}/s, "
                      f"eta {(len(mints)-i-1)/rate/60:.0f}m)", flush=True)
    return got


def main() -> None:
    conn = get_connection()
    todo = [r[0] for r in conn.execute(
        """SELECT ge.token_mint FROM graduation_events ge
           LEFT JOIN tokens t ON t.mint = ge.token_mint
           WHERE t.created_on IS NULL""")]
    print(f"mints needing a createdOn fetch: {len(todo)}")
    got = asyncio.run(fetch_all(todo))

    fetched = {m: co for m, co in got.items() if co is not None}
    conn.executemany("UPDATE tokens SET created_on=? WHERE mint=?",
                     [(co, m) for m, co in fetched.items()])
    conn.commit()
    failed = sum(1 for co in got.values() if co is None)
    print(f"persisted {len(fetched)} createdOn values ({failed} fetch failures — left for retry)")

    # classify EVERY graduation off the persisted column (older rows included)
    rows = conn.execute(
        """SELECT ge.token_mint m, t.created_on co FROM graduation_events ge
           LEFT JOIN tokens t ON t.mint = ge.token_mint""").fetchall()
    bad = [r["m"] for r in rows
           if not _is_pump_fun_token(r["co"] or None, r["m"]) and (r["co"] or "") != ""]
    # unresolved (fetch failed AND non-pump suffix) — report, do not purge blind
    unresolved = [r["m"] for r in rows
                  if (r["co"] is None or r["co"] == "") and not r["m"].lower().endswith("pump")]
    print(f"non-pump.fun graduations found: {len(bad)}   unresolved (retry later): {len(unresolved)}")

    platforms = {}
    for r in rows:
        if r["m"] in set(bad):
            platforms[r["co"]] = platforms.get(r["co"], 0) + 1
    for co, n in sorted(platforms.items(), key=lambda x: -x[1]):
        print(f"  purging {co!r}: {n}")

    for t in PURGE_TABLES:
        n = 0
        for m in bad:
            try:
                n += conn.execute(f"DELETE FROM {t} WHERE token_mint=?", (m,)).rowcount
            except Exception:
                pass
        if n:
            print(f"  {t}: -{n}")
    conn.commit()
    conn.close()
    print("purge done — rebuilding aggregates")

    import subprocess
    subprocess.run([sys.executable, "scripts/rebuild_funder_lineage.py"], cwd=Path(__file__).parent.parent)
    subprocess.run([sys.executable, "scripts/track_record.py"], cwd=Path(__file__).parent.parent)
    print("SWEEP COMPLETE")


if __name__ == "__main__":
    main()
