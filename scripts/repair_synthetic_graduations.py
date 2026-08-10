"""Remove graduation records whose tape predates the recorded graduation.

Cause: graduation_events was written with INSERT OR REPLACE, so re-analysing an
already-graduated coin stamped `now` over the original graduation moment. The
2026-08-09 outage recovery re-ran 162 stranded coins through that path, which
moved their graduation weeks forward and swept in tokens that never graduated on
pump.fun at all (USDC, and a mint created in 2024).

Every label we learn from is anchored on graduated_at — trajectory, team-exit
timing, collapse — so a coin whose own tape starts BEFORE its graduation has an
unusable anchor. The true moment is not recoverable from what we stored, so these
coins are removed rather than guessed at.

The write-once fix in graduation_monitor.py prevents recurrence; this cleans up
the rows that fix cannot retroactively repair.

    uv run python scripts/repair_synthetic_graduations.py [--apply]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

TABLES = (
    "graduation_events", "coin_trajectory", "team_clusters", "team_members",
    "post_grad_swaps", "post_grad_behavior", "graduation_feature_snapshot",
    "token_classification", "early_attention", "early_predictions",
    "model_predictions", "team_dump_alerts", "prewarn_alerts", "coin_outcomes",
    "holder_snapshots", "graduation_market", "hazard_predictions",
)

# 10 min of slack: a real tape can open a few seconds before our detected moment.
SLACK_S = 600


def main() -> None:
    apply = "--apply" in sys.argv
    conn = get_connection()
    bogus = [r[0] for r in conn.execute(
        """SELECT g.token_mint FROM graduation_events g
           JOIN post_grad_swaps p USING(token_mint)
           GROUP BY g.token_mint
           HAVING MIN(p.ts) < g.graduated_at - ?""", (SLACK_S,))]
    print(f"{len(bogus)} coins with a tape predating their graduation")
    if not bogus or not apply:
        print("dry run — pass --apply to delete" if bogus else "nothing to do")
        conn.close()
        return

    total = 0
    for t in TABLES:
        try:
            total += conn.executemany(
                f"DELETE FROM {t} WHERE token_mint = ?", [(m,) for m in bogus]).rowcount
        except Exception:
            continue                      # table absent in this schema version
    conn.commit()
    left = conn.execute(
        """SELECT COUNT(*) FROM (SELECT g.token_mint FROM graduation_events g
           JOIN post_grad_swaps p USING(token_mint) GROUP BY g.token_mint
           HAVING MIN(p.ts) < g.graduated_at - ?)""", (SLACK_S,)).fetchone()[0]
    print(f"deleted {total} rows; remaining violations: {left}")
    conn.close()


if __name__ == "__main__":
    main()
