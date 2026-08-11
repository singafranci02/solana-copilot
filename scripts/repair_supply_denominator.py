"""Recompute team supply_pct for coins whose denominator was the 1e9 assumption.

extract_total_supply falls back to a hardcoded 1e9 whenever Solana Tracker omits
pools[].tokenSupply, which became the norm once the pipeline went RPC-first. Real
supplies are not 1e9 — burns push them below it and some mints run to ~2e9 — so
every supply_pct computed against the assumption is mis-scaled. Coins whose team
then "held" more than 100% of supply are the visible tip of that.

The stored ui_amounts are real; only the denominator was wrong, so the pct is
recoverable. Caveat worth stating: getTokenSupply returns supply NOW, not supply
at graduation, so this is a correction rather than a perfect reconstruction — but
a measured denominator beats an assumed one that produces impossible values.

Forward-fixed in graduation_monitor.py (chain-first read); this repairs history.

    uv run python scripts/repair_supply_denominator.py [--apply]
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection


async def _chain_supplies(mints: list[str]) -> dict[str, float]:
    import aiohttp

    from src.ingest.graduation_monitor import _RPC_ENDPOINTS
    from src.ingest.rpc_holders import get_token_supply_rpc

    out: dict[str, float] = {}
    async with aiohttp.ClientSession() as s:
        for m in mints:
            v = await get_token_supply_rpc(s, m, _RPC_ENDPOINTS)
            if v:
                out[m] = v
            await asyncio.sleep(0.2)
    return out


def main() -> None:
    apply = "--apply" in sys.argv
    conn = get_connection()
    rows = conn.execute(
        """SELECT tc.rowid rid, tc.token_mint m, tc.member_addresses ma,
                  tc.supply_pct_at_graduation p, ge.bc_top_holders_json j
           FROM team_clusters tc JOIN graduation_events ge USING(token_mint)
           WHERE tc.supply_pct_at_graduation > 100""").fetchall()
    if not rows:
        print("no impossible supply_pct rows")
        conn.close()
        return

    mints = [r["m"] for r in rows]
    supplies = asyncio.run(_chain_supplies(mints))

    updates, hupdates = [], []
    for r in rows:
        real = supplies.get(r["m"])
        if not real:
            print(f"  {r['m'][:10]} — chain supply unavailable, left alone")
            continue
        holders = json.loads(r["j"] or "[]")
        if not holders:
            continue
        # the denominator actually used, inferred from a stored (amount, pct) pair
        used = next((h["ui_amount"] / (h["pct"] / 100)
                     for h in holders if h.get("pct")), None)
        if not used:
            continue
        scale = used / real
        members = set(json.loads(r["ma"] or "[]"))
        fixed = [dict(h, pct=round(h["pct"] * scale, 2)) for h in holders]
        new_pct = round(sum(h["pct"] for h in fixed if h["wallet"] in members), 2)
        print(f"  {r['m'][:10]} supply {used:,.0f} -> {real:,.0f}   "
              f"team {r['p']:.1f}% -> {new_pct:.1f}%")
        updates.append((new_pct, r["rid"]))
        hupdates.append((json.dumps(fixed), r["m"]))

    if not apply:
        print("\ndry run — pass --apply to write")
        conn.close()
        return

    conn.executemany(
        "UPDATE team_clusters SET supply_pct_at_graduation = ? WHERE rowid = ?", updates)
    conn.executemany(
        "UPDATE graduation_events SET bc_top_holders_json = ? WHERE token_mint = ?", hupdates)
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM team_clusters WHERE supply_pct_at_graduation > 100").fetchone()[0]
    print(f"\nrepaired {len(updates)} clusters; remaining >100%: {left}")
    conn.close()


if __name__ == "__main__":
    main()
