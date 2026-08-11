"""Rebuild coin_trajectory from the stored tape.

Needed after anything that changes the tape or its flags — notably
repair_tape_team_flags.py, since time_to_team_exit_s is the first sell where
is_team=1 and is therefore only as correct as those marks.

Pure recompute from data already in the DB: no API calls.

    uv run python scripts/recompute_trajectories.py [--limit N]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.trajectory import trajectory_from_db, upsert_trajectory
from src.common.db import get_connection


def main() -> None:
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    conn = get_connection()
    q = """SELECT ct.token_mint m, ge.graduated_at g
           FROM coin_trajectory ct JOIN graduation_events ge USING(token_mint)
           ORDER BY ge.graduated_at DESC"""
    if limit:
        q += f" LIMIT {limit}"
    rows = conn.execute(q).fetchall()
    print(f"recomputing {len(rows)} trajectories from the stored tape")

    changed = 0
    for i, r in enumerate(rows, 1):
        before = conn.execute(
            "SELECT time_to_team_exit_s FROM coin_trajectory WHERE token_mint = ?",
            (r["m"],)).fetchone()
        t = trajectory_from_db(conn, r["m"], int(r["g"]))
        upsert_trajectory(conn, t)
        if (before[0] if before else None) != t.time_to_team_exit_s:
            changed += 1
        if i % 250 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)} ({changed} exit times changed)")
    conn.commit()
    print(f"done — {changed} of {len(rows)} coins had their team-exit time change")
    conn.close()


if __name__ == "__main__":
    main()
