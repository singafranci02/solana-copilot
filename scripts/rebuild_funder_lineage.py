"""One-shot rebuild of everything keyed on the team's FUNDER, under gated membership.

The funder was resolved one hop back from the OLD bloated member lists, so 54% of
stored funding_sources are no longer linked to any gated member — we were compounding
reputation onto wallets that funded NOISE, not teams. Likewise team_member_behavior
(the exit-choreography memory) was 88% non-gated rows. This rebuilds, from already-
stored data only (zero API calls):

  1. team_clusters.funding_source  — most common non-CEX funder among GATED members
  2. team_member_behavior          — non-gated wallet rows deleted
  3. team_fingerprints choreography rollups (leader/spread/consistency) recomputed
  4. funder_reputation counters    — re-aggregated from gated clusters + outcomes;
     is_known_rugger re-derived under the same gates (n>=8 AND rug_rate>=0.65)

Velocity columns (launches_24h/7d) are live measurements and are left untouched.

    uv run python scripts/rebuild_funder_lineage.py
"""

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

RUGGER_MIN_MINTS = 8
RUGGER_MIN_RATE = 0.65


def main() -> None:
    conn = get_connection()
    t0 = time.time()

    clusters = {r["token_mint"]: set(json.loads(r["member_addresses"] or "[]"))
                for r in conn.execute("SELECT token_mint, member_addresses FROM team_clusters")}

    funder_of: dict[str, str] = {}
    for r in conn.execute("SELECT wallet, funder FROM wallet_funding WHERE hop = 1"):
        if r["funder"] and r["funder"] != "cex":
            funder_of[r["wallet"]] = r["funder"]

    # 1. re-resolve funding_source from gated members only
    changed = cleared = 0
    updates = []
    for mint, members in clusters.items():
        counts = Counter(funder_of[w] for w in members if w in funder_of)
        new = counts.most_common(1)[0][0] if counts else None
        updates.append((new, mint))
    for new, mint in updates:
        cur = conn.execute("SELECT funding_source FROM team_clusters WHERE token_mint=?",
                           (mint,)).fetchone()
        if cur and cur["funding_source"] != new:
            conn.execute("UPDATE team_clusters SET funding_source=? WHERE token_mint=?",
                         (new, mint))
            changed += 1
            if new is None:
                cleared += 1
    conn.commit()
    print(f"funding_source: {changed} re-pointed ({cleared} cleared — no gated member traceable)")

    # 2. purge non-gated behaviour rows
    n = conn.execute("""DELETE FROM team_member_behavior
        WHERE NOT EXISTS (SELECT 1 FROM team_members tm
            WHERE tm.token_mint = team_member_behavior.token_mint
              AND tm.wallet = team_member_behavior.wallet AND tm.is_member = 1)""").rowcount
    conn.commit()
    print(f"team_member_behavior: {n:,} non-gated rows deleted")

    # 3. choreography rollups per (new) funder, from gated rows only
    funder_by_mint = {r["token_mint"]: r["funding_source"] for r in conn.execute(
        "SELECT token_mint, funding_source FROM team_clusters WHERE funding_source IS NOT NULL")}
    per_funder: dict[str, list] = defaultdict(list)
    for r in conn.execute("""SELECT token_mint, wallet, first_sell_offset_s, is_first_seller
                             FROM team_member_behavior WHERE first_sell_offset_s IS NOT NULL"""):
        f = funder_by_mint.get(r["token_mint"])
        if f:
            per_funder[f].append(r)
    conn.execute("""UPDATE team_fingerprints SET avg_exit_spread_s=NULL, leader_wallet=NULL,
                    leader_consistency=NULL, choreography_sample_count=0""")
    rolled = 0
    for f, rows in per_funder.items():
        by_coin: dict[str, list] = defaultdict(list)
        for r in rows:
            by_coin[r["token_mint"]].append(r)
        coins = {m: v for m, v in by_coin.items() if len(v) >= 2}
        if not coins:
            continue
        spreads, firsts = [], []
        for v in coins.values():
            offs = [x["first_sell_offset_s"] for x in v]
            spreads.append(max(offs) - min(offs))
            firsts.append(min(v, key=lambda x: x["first_sell_offset_s"])["wallet"])
        leader, lead_n = Counter(firsts).most_common(1)[0]
        conn.execute("""UPDATE team_fingerprints SET avg_exit_spread_s=?, leader_wallet=?,
                        leader_consistency=?, choreography_sample_count=? WHERE funding_source=?""",
                     (sum(spreads) / len(spreads), leader, lead_n / len(coins), len(coins), f))
        rolled += 1
    conn.commit()
    print(f"team_fingerprints: choreography rolled up for {rolled} funders (gated rows only)")

    # 4. funder_reputation from gated clusters + 4h outcomes.
    # CONTRACT: graduated_mints is a JSON ARRAY of mints — rules.py does
    # len(rep.graduated_mints) in the hard-SKIP path and smart_money.py appends to
    # it for dedup. Writing an integer here once crashed the LIVE verdict path
    # (TypeError) for ~90 minutes. Every row must hold a valid array.
    agg: dict[str, dict] = defaultdict(lambda: {"mints": [], "rug": 0, "moon": 0, "ok": 0})
    for r in conn.execute("""
        SELECT tc.funding_source f, tc.token_mint m, co.classified c
        FROM team_clusters tc JOIN coin_outcomes co ON co.token_mint = tc.token_mint
        WHERE tc.funding_source IS NOT NULL AND co.check_offset_h = 4
          AND co.classified IN ('moon','ok','rug')"""):
        a = agg[r["f"]]
        if r["m"] not in a["mints"]:
            a["mints"].append(r["m"])
            a[r["c"]] += 1
    conn.execute("""UPDATE funder_reputation SET graduated_mints='[]', rug_count=0,
                    moon_count=0, ok_count=0, rug_rate=0, is_known_rugger=0""")
    flagged = 0
    for f, a in agg.items():
        total = len(a["mints"])
        rate = a["rug"] / total if total else 0.0
        rugger = int(total >= RUGGER_MIN_MINTS and rate >= RUGGER_MIN_RATE)
        flagged += rugger
        conn.execute("""INSERT INTO funder_reputation
                            (funding_source, graduated_mints, rug_count, moon_count,
                             ok_count, rug_rate, last_seen, is_known_rugger)
                        VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(funding_source) DO UPDATE SET
                            graduated_mints=excluded.graduated_mints,
                            rug_count=excluded.rug_count, moon_count=excluded.moon_count,
                            ok_count=excluded.ok_count, rug_rate=excluded.rug_rate,
                            is_known_rugger=excluded.is_known_rugger""",
                     (f, json.dumps(a["mints"]), a["rug"], a["moon"], a["ok"], rate,
                      int(time.time()), rugger))
    conn.commit()
    print(f"funder_reputation: {len(agg)} funders re-aggregated, {flagged} is_known_rugger "
          f"(n>={RUGGER_MIN_MINTS}, rate>={RUGGER_MIN_RATE})")
    print(f"done in {time.time()-t0:.0f}s")
    conn.close()


if __name__ == "__main__":
    main()
