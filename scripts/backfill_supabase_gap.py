"""One-off: push local rows to Supabase that failed to sync during the v2
rollout window (missing-column 400s + FK-cascade 409s, before the DDL ran).

Pushes tokens first (FK parent), then graduation_events, team_clusters,
token_classification, coin_outcomes, post_grad_behavior for every graduation
newer than the newest row already in Supabase (or --since <epoch>).

Usage: uv run python scripts/backfill_supabase_gap.py [--since EPOCH]
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.common import supabase_sync as sb


async def main() -> None:
    conn = get_connection()
    try:
        if "--since" in sys.argv:
            since = int(sys.argv[sys.argv.index("--since") + 1])
        else:
            client = sb._get_client()
            assert client is not None, "Supabase not configured"
            res = client.table("graduation_events").select("graduated_at") \
                .order("graduated_at", desc=True).limit(1).execute()
            since = int(res.data[0]["graduated_at"]) if res.data else 0
        print(f"backfilling graduations with graduated_at > {since}")

        rows = conn.execute(
            """SELECT ge.*, t.symbol, t.name, t.created_at, t.created_at_source,
                      t.creator_wallet, t.total_supply
               FROM graduation_events ge JOIN tokens t ON t.mint = ge.token_mint
               WHERE ge.graduated_at > ? ORDER BY ge.graduated_at""",
            (since,),
        ).fetchall()
        print(f"{len(rows)} graduations to push")

        for r in rows:
            mint = r["token_mint"]
            await sb.token(
                mint, r["symbol"] or "?", r["name"] or "?", int(r["created_at"]),
                created_at_source=r["created_at_source"],
                creator_wallet=r["creator_wallet"],
                total_supply=r["total_supply"],
            )
            await sb.graduation_event(
                token_mint=mint,
                graduated_at=int(r["graduated_at"]),
                detection_lag_seconds=int(r["detection_lag_seconds"] or 0),
                structural_verdict=r["structural_verdict"],
                verdict_confidence=float(r["verdict_confidence"] or 0),
                pumpswap_pool_address=r["amm_pool_address"] or r["pumpswap_pool_address"],
                bc_top_holders_json=json.loads(r["bc_top_holders_json"] or "[]"),
                smart_money_count=int(r["smart_money_count"] or 0),
                dominant_factors_json=json.loads(r["dominant_factors_json"] or "[]"),
                pipeline_version=int(r["pipeline_version"] or 1),
            )
            tc = conn.execute(
                "SELECT * FROM team_clusters WHERE token_mint = ?", (mint,)
            ).fetchone()
            if tc:
                await sb.team_cluster(
                    cluster_id=tc["cluster_id"], token_mint=mint,
                    funding_source=tc["funding_source"],
                    member_addresses=json.loads(tc["member_addresses"] or "[]"),
                    supply_pct_at_graduation=float(tc["supply_pct_at_graduation"] or 0),
                    first_buy_offset_seconds=float(tc["first_buy_offset_seconds"] or 0),
                    is_bc_sniper=bool(tc["is_bc_sniper"]),
                )
            cls = conn.execute(
                "SELECT * FROM token_classification WHERE token_mint = ?", (mint,)
            ).fetchone()
            if cls:
                await sb.token_classification(
                    token_mint=mint, label=cls["label"],
                    is_project=bool(cls["is_project"]),
                    confidence=float(cls["confidence"] or 0), reason=cls["reason"],
                    has_website=bool(cls["has_website"]), website=cls["website"],
                    twitter=cls["twitter"], telegram=cls["telegram"],
                    description=cls["description"],
                )
            for o in conn.execute(
                "SELECT * FROM coin_outcomes WHERE token_mint = ?", (mint,)
            ):
                await sb.coin_outcome(
                    token_mint=mint, check_offset_h=int(o["check_offset_h"]),
                    checked_at=int(o["checked_at"]), mc_usd=o["mc_usd"],
                    price_change_pct=o["price_change_pct"], classified=o["classified"],
                )
            for b in conn.execute(
                "SELECT * FROM post_grad_behavior WHERE token_mint = ?", (mint,)
            ):
                await sb.post_grad_behavior(
                    token_mint=mint, check_offset_h=int(b["check_offset_h"]),
                    checked_at=int(b["checked_at"]),
                    holders_remaining_count=b["holders_remaining_count"],
                    team_sold_pct=b["team_sold_pct"],
                    distribution_signal=b["distribution_signal"],
                    snipers_sold_pct=b["snipers_sold_pct"],
                    liquidity_usd=b["liquidity_usd"],
                    team_buy_count=int(b["team_buy_count"] or 0),
                    team_sell_count=int(b["team_sell_count"] or 0),
                    team_net_sol=b["team_net_sol"],
                    coordinated_sell_count=int(b["coordinated_sell_count"] or 0),
                )
            print(f"  pushed {mint[:8]}")
        print("done")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
