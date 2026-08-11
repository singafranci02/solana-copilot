"""Re-stamp is_team/is_sniper on the swap tape from actual cluster membership.

backfill_post_grad_swaps.py called upsert_swaps(..., is_team=True), but
fetch_team_swaps returns the WHOLE tape rather than only the wallets passed to
it — so every trader on a backfilled coin was flagged as a team member.

That flag is not cosmetic: trajectory.py reads time_to_team_exit_s as the first
sell where is_team=1, so on an affected coin the "team exit" is really the first
sell by anyone. Team-exit timing is one of the system's headline signals, so the
labels have to be rebuilt from membership and the timing re-measured afterwards.

Run --apply, then recompute trajectories so the labels pick up the corrected
flags:  uv run python scripts/recompute_trajectories.py   (or the retrain job)

    uv run python scripts/repair_tape_team_flags.py [--apply]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.distribution import _apply_tape_flags
from src.common.db import get_connection


def main() -> None:
    apply = "--apply" in sys.argv
    conn = get_connection()
    rows = conn.execute(
        """SELECT tc.token_mint m, tc.member_addresses ma, tc.is_bc_sniper sn
           FROM team_clusters tc
           WHERE EXISTS (SELECT 1 FROM post_grad_swaps p
                         WHERE p.token_mint = tc.token_mint)""").fetchall()

    affected = conn.execute(
        """SELECT COUNT(DISTINCT p.token_mint) FROM post_grad_swaps p
           JOIN team_clusters tc ON tc.token_mint = p.token_mint
           WHERE p.is_team = 1
             AND instr(tc.member_addresses, p.wallet_address) = 0""").fetchone()[0]
    print(f"{len(rows)} coins with a tape; {affected} carry is_team marks outside "
          f"their cluster")
    if not apply:
        print("dry run — pass --apply to re-stamp")
        conn.close()
        return

    smart = set()
    try:
        smart = {r[0] for r in conn.execute(
            "SELECT address FROM wallets WHERE smart_money_score >= 1")}
    except Exception:
        pass

    for r in rows:
        members = set(json.loads(r["ma"] or "[]"))
        snipers = members if r["sn"] else set()
        _apply_tape_flags(conn, r["m"], members, snipers, smart)
    conn.commit()

    left = conn.execute(
        """SELECT COUNT(DISTINCT p.token_mint) FROM post_grad_swaps p
           JOIN team_clusters tc ON tc.token_mint = p.token_mint
           WHERE p.is_team = 1
             AND instr(tc.member_addresses, p.wallet_address) = 0""").fetchone()[0]
    print(f"re-stamped {len(rows)} coins; coins still mismatched: {left}")
    print("NOW recompute trajectories — time_to_team_exit_s is derived from is_team")
    conn.close()


if __name__ == "__main__":
    main()
