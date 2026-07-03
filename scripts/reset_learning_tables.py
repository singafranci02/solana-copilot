"""ONE-TIME learning-table reset at pipeline v2 deploy (2026-07).

Before v2, pool/curve addresses were counted as team members, poisoning
wallet_graph (false rug co-appearances → false hard-SKIPs), funder_reputation,
and team_fingerprints. Raw observational tables are kept; memory re-matures
from clean v2 data in weeks at ~300 graduations/day.

Mirror on Supabase: db/migrations/2026-07-pipeline-v2-reset.sql (manual).

Usage: uv run python scripts/reset_learning_tables.py --yes
"""

import sys

from src.common.db import get_connection

TABLES = ("wallet_graph", "funder_reputation", "team_fingerprints", "wallet_stats")


def main() -> None:
    if "--yes" not in sys.argv:
        print(f"Would DELETE all rows from: {', '.join(TABLES)}")
        print("Re-run with --yes to execute.")
        return
    conn = get_connection()
    try:
        for table in TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            print(f"{table}: deleted {n} rows")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
