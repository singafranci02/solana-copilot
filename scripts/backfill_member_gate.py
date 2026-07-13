"""One-shot backfill: re-apply the skin-in-the-game member gate to history.

The old score-only membership admitted an average of 86 wallets/coin (max 628) via
additive weak evidence; ground-truthed against the tape those extras were 9.8%
insiders (vs 26.7% for gated members) and 75% never sold at all. Because
`post_grad_swaps.is_team` and the trajectory's team-exit fields were derived from
that bloated set, THE LABELS THEMSELVES are polluted — "team first sell" often meant
"first sell by any of 86 wallets". This script repairs everything derived from
membership, using the frozen evidence_json (no API calls, pure re-derivation):

  1. team_members.is_member       — re-gated via passes_member_gate
  2. team_clusters                — member_addresses + supply_pct recomputed
  3. post_grad_swaps.is_team      — re-marked from the gated set
  4. coin_trajectory              — time_to_team_exit_s / team_leads_collapse recomputed

Feature snapshots are NOT touched: they are point-in-time records of what the system
believed at graduation, and rewriting them would be leakage in the other direction.
Coins where the gate empties the team keep their old member list (mirrors the live
top-5 fallback).

    uv run python scripts/backfill_member_gate.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.team_detect import _MEMBER_THRESHOLD, passes_member_gate
from src.analyzer.trajectory import trajectory_from_db, upsert_trajectory
from src.common.db import get_connection


def main() -> None:
    conn = get_connection()
    grad = {r["token_mint"]: int(r["graduated_at"]) for r in conn.execute(
        "SELECT token_mint, graduated_at FROM graduation_events")}
    holders = {}
    for r in conn.execute(
            "SELECT token_mint, bc_top_holders_json FROM graduation_events"):
        try:
            holders[r["token_mint"]] = {
                h["wallet"]: h.get("pct", 0.0) for h in json.loads(r["bc_top_holders_json"] or "[]")}
        except Exception:
            holders[r["token_mint"]] = {}

    rows: dict[str, list] = {}
    for r in conn.execute(
            "SELECT token_mint, wallet, score, is_member, evidence_json FROM team_members"):
        rows.setdefault(r["token_mint"], []).append(
            (r["wallet"], r["score"], r["is_member"], r["evidence_json"]))

    t0 = time.time()
    n_coins = n_shrunk = n_kept_fallback = n_traj = 0
    old_total = new_total = 0
    for i, (mint, wallets) in enumerate(rows.items()):
        new_members = set()
        flips = []                       # (new_is_member, wallet)
        for w, score, old_m, ev_json in wallets:
            try:
                ev = json.loads(ev_json or "{}")
            except Exception:
                ev = {}
            keep = int(score >= _MEMBER_THRESHOLD and passes_member_gate(ev))
            if keep:
                new_members.add(w)
            if keep != old_m:
                flips.append((keep, w))
            old_total += old_m
        new_total += len(new_members)

        old_members = {w for w, _, m, _ in wallets if m}
        if not new_members and old_members:
            n_kept_fallback += 1         # mirror the live top-5 fallback: keep old set
            continue

        if flips:
            conn.executemany(
                "UPDATE team_members SET is_member=? WHERE token_mint=? AND wallet=?",
                [(k, mint, w) for k, w in flips])
        if new_members != old_members:
            n_shrunk += 1
            hm = holders.get(mint, {})
            supply = round(sum(hm.get(w, 0.0) for w in new_members), 2)
            conn.execute(
                "UPDATE team_clusters SET member_addresses=?, supply_pct_at_graduation=? "
                "WHERE token_mint=?",
                (json.dumps(sorted(new_members)), supply, mint))
            # re-mark the tape, then recompute the trajectory labels off it
            conn.execute("UPDATE post_grad_swaps SET is_team=0 WHERE token_mint=? AND is_team=1",
                         (mint,))
            conn.executemany(
                "UPDATE post_grad_swaps SET is_team=1 WHERE token_mint=? AND wallet_address=?",
                [(mint, w) for w in new_members])
            if mint in grad:
                upsert_trajectory(conn, trajectory_from_db(conn, mint, grad[mint]))
                n_traj += 1
        conn.commit()
        n_coins += 1
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(rows)} coins  ({time.time()-t0:.0f}s)")

    print(f"\ndone in {time.time()-t0:.0f}s")
    print(f"coins processed      : {n_coins}  (of {len(rows)})")
    print(f"membership changed   : {n_shrunk}")
    print(f"kept top-5 fallback  : {n_kept_fallback}  (gate emptied the team)")
    print(f"trajectories rebuilt : {n_traj}")
    print(f"member rows          : {old_total:,} -> {new_total:,}  "
          f"({new_total/max(old_total,1):.0%} of old)")
    conn.close()


if __name__ == "__main__":
    main()
