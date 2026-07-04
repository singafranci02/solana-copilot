"""Export the training dataset: one row per clean (pipeline_version >= 2)
graduation, joining the point-in-time feature snapshot with BC flow features,
launch coordination, classification, and outcome labels.

Leak rules:
  - Features come ONLY from graduation_feature_snapshot (frozen at verdict
    time), bc_flow_features, coin_coordination(phase='launch'), and
    token_classification — all computed at/before graduation.
  - Labels (outcome_1h/4h/24h, distribution signals, price changes) are the
    supervised targets, never features.
  - pipeline_version=1 rows are excluded (pool-contaminated).

Usage:
    uv run python scripts/export_training_dataset.py [out.csv]
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

LABEL_OFFSETS = (1, 4, 24)


def export(out_path: str) -> int:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT gfs.token_mint, gfs.features_json, gfs.snapped_at,
                      ge.graduated_at,
                      bff.n_trades, bff.n_buyers, bff.n_sellers, bff.buys_first_60s,
                      bff.same_second_bundle_count, bff.top5_buyer_share,
                      bff.gini_buy_size, bff.sol_in, bff.sol_out,
                      bff.launch_slot_snipe_count, bff.buys_first_slot,
                      bff.buys_first_3_slots, bff.distinct_slots_first_20_buys,
                      bff.max_same_slot_group, bff.bundled_adjacent_count,
                      cc.bundled_supply_pct, cc.largest_entity_supply_pct,
                      cc.largest_entity_wallet_count, cc.largest_entity_fresh_ratio,
                      tcl.is_project, tcl.has_website
               FROM graduation_feature_snapshot gfs
               JOIN graduation_events ge
                 ON ge.token_mint = gfs.token_mint AND ge.pipeline_version >= 2
               LEFT JOIN bc_flow_features bff ON bff.token_mint = gfs.token_mint
               LEFT JOIN coin_coordination cc
                 ON cc.token_mint = gfs.token_mint AND cc.phase = 'launch'
               LEFT JOIN token_classification tcl ON tcl.token_mint = gfs.token_mint
               ORDER BY ge.graduated_at""",
        ).fetchall()

        out_rows = []
        for r in rows:
            row: dict = {"token_mint": r["token_mint"], "graduated_at": r["graduated_at"]}
            row.update(json.loads(r["features_json"] or "{}"))
            for col in (
                "n_trades", "n_buyers", "n_sellers", "buys_first_60s",
                "same_second_bundle_count", "top5_buyer_share", "gini_buy_size",
                "sol_in", "sol_out", "launch_slot_snipe_count", "buys_first_slot",
                "buys_first_3_slots", "distinct_slots_first_20_buys",
                "max_same_slot_group", "bundled_adjacent_count", "bundled_supply_pct",
                "largest_entity_supply_pct", "largest_entity_wallet_count",
                "largest_entity_fresh_ratio", "is_project", "has_website",
            ):
                row[col] = r[col]

            # Exit-choreography labels (team_member_behavior aggregated per coin)
            ch = conn.execute(
                """SELECT COUNT(*) n, MIN(first_sell_offset_s) lead_sell_s,
                          MAX(exit_order) last_order
                   FROM team_member_behavior
                   WHERE token_mint = ? AND exit_order IS NOT NULL""",
                (r["token_mint"],),
            ).fetchone()
            row["label_team_sellers"] = ch["n"] if ch else None
            row["label_leader_first_sell_s"] = ch["lead_sell_s"] if ch else None

            # Labels — supervised targets only
            for off in LABEL_OFFSETS:
                o = conn.execute(
                    """SELECT classified, price_change_pct FROM coin_outcomes
                       WHERE token_mint = ? AND check_offset_h = ?""",
                    (r["token_mint"], off),
                ).fetchone()
                row[f"label_outcome_{off}h"] = o["classified"] if o else None
                row[f"label_change_pct_{off}h"] = o["price_change_pct"] if o else None
                b = conn.execute(
                    """SELECT distribution_signal, team_sold_pct FROM post_grad_behavior
                       WHERE token_mint = ? AND check_offset_h = ?""",
                    (r["token_mint"], off),
                ).fetchone()
                row[f"label_dist_signal_{off}h"] = b["distribution_signal"] if b else None
                row[f"label_team_sold_pct_{off}h"] = b["team_sold_pct"] if b else None
            out_rows.append(row)

        if not out_rows:
            print("no exportable rows yet (need pipeline_version>=2 graduations with snapshots)")
            return 0

        fieldnames = sorted({k for row in out_rows for k in row})
        # stable, readable ordering: ids first, labels last
        ids = ["token_mint", "graduated_at"]
        labels = sorted(f for f in fieldnames if f.startswith("label_"))
        feats = [f for f in fieldnames if f not in ids and f not in labels]
        with open(out_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=ids + feats + labels)
            w.writeheader()
            w.writerows(out_rows)
        print(f"wrote {len(out_rows)} rows → {out_path}")
        return len(out_rows)
    finally:
        conn.close()


if __name__ == "__main__":
    export(sys.argv[1] if len(sys.argv) > 1 else "training_dataset.csv")
