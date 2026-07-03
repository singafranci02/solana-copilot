"""Team memory system — cross-launch behavioral fingerprinting.

Tracks how teams behave across multiple launches even when they rotate wallets.
Four memory layers, each surviving a different evasion level:

  Layer 1 (wallet_graph):      catches 1-2 recycled wallets
  Layer 2 (launch velocity):   catches same funder launching pump rings
  Layer 3 (dump timing):       learns WHEN a funder typically starts selling
  Layer 4 (fingerprint match): catches total wallet+funder rotation via structural similarity

All functions are pure (no module-level IO). Every write is incremental
— no full recomputes. Thread-safe for concurrent SQLite WAL access.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from src.common.models import FingerprintMatch, MemorySignals, TeamCluster, WalletGraphHit

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

# Feature vector normalization (fixed — tune without schema changes)
_NORM_CLUSTER_SIZE = 20.0
_NORM_SUPPLY_PCT   = 100.0
_NORM_OFFSET_S     = 300.0

_FINGERPRINT_MATCH_DISTANCE = 0.25   # L2 distance threshold for structural match
_MIN_FINGERPRINT_RUG_RATE   = 0.6
_MIN_FINGERPRINT_SAMPLE     = 4

_MIN_DUMP_START_OBSERVATIONS = 3     # avg_dump_start_h not surfaced until this count
_PUMP_RING_VELOCITY_24H      = 3     # launches/24h that flags a pump ring


# ── Layer 1: wallet co-occurrence graph ───────────────────────────────────────

def update_wallet_graph(
    member_addresses: list[str],
    outcome: Optional[str],  # "rug" | "moon" | "ok" | None (at graduation time)
    conn,
) -> None:
    """Upsert all wallet pairs from a team cluster into the co-occurrence graph.

    Called twice per token:
      1. At graduation (outcome=None) — registers the cluster.
      2. At 4h outcome — if outcome="rug", increments rug_co_appearances.
    """
    now = int(time.time())
    is_rug = outcome == "rug"

    pairs = _canonical_pairs(member_addresses)
    if not pairs:
        return

    for wallet_a, wallet_b in pairs:
        if is_rug:
            conn.execute(
                """INSERT INTO wallet_graph
                       (wallet_a, wallet_b, co_appearances, rug_co_appearances, last_seen_together)
                   VALUES (?, ?, 1, 1, ?)
                   ON CONFLICT(wallet_a, wallet_b) DO UPDATE SET
                       co_appearances     = co_appearances + 1,
                       rug_co_appearances = rug_co_appearances + 1,
                       last_seen_together = excluded.last_seen_together""",
                (wallet_a, wallet_b, now),
            )
        else:
            conn.execute(
                """INSERT INTO wallet_graph
                       (wallet_a, wallet_b, co_appearances, rug_co_appearances, last_seen_together)
                   VALUES (?, ?, 1, 0, ?)
                   ON CONFLICT(wallet_a, wallet_b) DO UPDATE SET
                       co_appearances     = co_appearances + 1,
                       last_seen_together = excluded.last_seen_together""",
                (wallet_a, wallet_b, now),
            )

    conn.commit()
    logger.debug(
        "wallet_graph: %d pairs upserted (rug=%s)", len(pairs), is_rug
    )
    _sync_graph_pairs(pairs, conn)


def _sync_graph_pairs(pairs: list[tuple[str, str]], conn) -> None:
    """Mirror the touched pairs to Supabase. No-op outside an event loop."""
    import asyncio
    from src.common import supabase_sync as sb

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # sync context (scripts/tests) — Supabase mirror skipped

    rows = []
    for wallet_a, wallet_b in pairs:
        r = conn.execute(
            """SELECT wallet_a, wallet_b, co_appearances, rug_co_appearances,
                      last_seen_together
               FROM wallet_graph WHERE wallet_a = ? AND wallet_b = ?""",
            (wallet_a, wallet_b),
        ).fetchone()
        if r:
            rows.append(dict(r))
    if rows:
        loop.create_task(sb.wallet_graph_pairs_batch(rows))


def query_wallet_graph(
    member_addresses: list[str],
    conn,
    min_co_appearances: int = 2,
) -> list[WalletGraphHit]:
    """Return graph hits: members that co-appeared with previously tracked wallets.

    Only returns rows where co_appearances >= min_co_appearances to prevent
    false positives from a single shared wallet.
    """
    if not member_addresses:
        return []

    placeholders = ",".join("?" * len(member_addresses))
    rows = conn.execute(
        f"""SELECT wallet_a, wallet_b, co_appearances, rug_co_appearances, last_seen_together
            FROM wallet_graph
            WHERE (wallet_a IN ({placeholders}) OR wallet_b IN ({placeholders}))
              AND co_appearances >= ?""",
        (*member_addresses, *member_addresses, min_co_appearances),
    ).fetchall()

    member_set = set(member_addresses)
    hits: list[WalletGraphHit] = []
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows:
        wa, wb = row["wallet_a"], row["wallet_b"]
        # Determine which side is in the current cluster vs. which is the "known" wallet
        if wa in member_set and wb not in member_set:
            connected, known = wa, wb
        elif wb in member_set and wa not in member_set:
            connected, known = wb, wa
        else:
            # Both in current cluster — internal pair, not an external link
            continue

        key = (connected, known)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        hits.append(WalletGraphHit(
            connected_wallet=connected,
            known_wallet=known,
            co_appearances=row["co_appearances"],
            rug_co_appearances=row["rug_co_appearances"],
            last_seen_together=row["last_seen_together"],
        ))

    return hits


# ── Layer 2: launch velocity ──────────────────────────────────────────────────

def update_launch_velocity(funding_source: Optional[str], conn) -> tuple[int, int]:
    """Recompute and persist launches_24h, launches_7d for a funder.

    Returns (launches_24h, launches_7d). Uses graduation_events joined to
    team_clusters to count this funder's recent launches efficiently.
    """
    if not funding_source or funding_source == "cex":
        return (0, 0)

    now = int(time.time())
    cutoff_24h = now - 86_400
    cutoff_7d  = now - 7 * 86_400

    row_24h = conn.execute(
        """SELECT COUNT(*) as n FROM graduation_events ge
           JOIN team_clusters tc ON tc.token_mint = ge.token_mint
           WHERE tc.funding_source = ? AND ge.graduated_at >= ?""",
        (funding_source, cutoff_24h),
    ).fetchone()

    row_7d = conn.execute(
        """SELECT COUNT(*) as n FROM graduation_events ge
           JOIN team_clusters tc ON tc.token_mint = ge.token_mint
           WHERE tc.funding_source = ? AND ge.graduated_at >= ?""",
        (funding_source, cutoff_7d),
    ).fetchone()

    launches_24h = int(row_24h["n"]) if row_24h else 0
    launches_7d  = int(row_7d["n"]) if row_7d else 0

    conn.execute(
        """INSERT INTO funder_reputation (funding_source, launches_24h, launches_7d, velocity_updated)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(funding_source) DO UPDATE SET
               launches_24h     = excluded.launches_24h,
               launches_7d      = excluded.launches_7d,
               velocity_updated = excluded.velocity_updated""",
        (funding_source, launches_24h, launches_7d, now),
    )
    conn.commit()

    return (launches_24h, launches_7d)


# ── Layer 3: distribution timing memory ──────────────────────────────────────

def record_dump_start(funding_source: Optional[str], offset_h: int, conn) -> None:
    """Record the first DISTRIBUTING signal offset for this funder, rolling average.

    Called once per token when the first DISTRIBUTING signal is detected.
    avg_dump_start_h is suppressed in signals until dump_start_count >= 3.
    """
    if not funding_source or funding_source == "cex":
        return

    row = conn.execute(
        "SELECT avg_dump_start_h, dump_start_count FROM funder_reputation WHERE funding_source = ?",
        (funding_source,),
    ).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO funder_reputation (funding_source, avg_dump_start_h, dump_start_count)
               VALUES (?, ?, 1)
               ON CONFLICT(funding_source) DO UPDATE SET
                   avg_dump_start_h = excluded.avg_dump_start_h,
                   dump_start_count = dump_start_count + 1""",
            (funding_source, float(offset_h)),
        )
    else:
        old_avg = float(row["avg_dump_start_h"] or offset_h)
        old_n   = int(row["dump_start_count"] or 0)
        new_n   = old_n + 1
        new_avg = (old_avg * old_n + offset_h) / new_n
        conn.execute(
            """UPDATE funder_reputation
               SET avg_dump_start_h = ?, dump_start_count = ?
               WHERE funding_source = ?""",
            (round(new_avg, 2), new_n, funding_source),
        )

    conn.commit()


# ── Layer 4: structural fingerprint ───────────────────────────────────────────

def update_fingerprint(
    team_cluster: TeamCluster,
    outcome: str,  # "rug" | "moon" | "ok"
    conn,
) -> None:
    """Update team_fingerprints rolling averages with a new outcome observation.

    Called at 4h alongside update_funder_reputation.
    """
    if not team_cluster.funding_source or team_cluster.funding_source == "cex":
        return

    funder = team_cluster.funding_source

    row = conn.execute(
        """SELECT avg_first_buy_offset_s, avg_sniper_rate, avg_bundle_pct,
                  avg_cluster_size, sample_count
           FROM team_fingerprints WHERE funding_source = ?""",
        (funder,),
    ).fetchone()

    cluster_size = float(len(team_cluster.member_addresses))
    is_sniper = 1.0 if team_cluster.is_bc_sniper else 0.0
    offset_s = float(team_cluster.first_buy_offset_seconds)
    supply_pct = float(team_cluster.supply_pct_at_graduation)

    if row is None:
        # Fresh insert — fingerprint_id is NOT NULL UNIQUE so it must be set here;
        # ON CONFLICT needs the UNIQUE index on funding_source (db.py migrate).
        import uuid
        conn.execute(
            """INSERT INTO team_fingerprints
               (fingerprint_id, funding_source, avg_first_buy_offset_s,
                avg_sniper_rate, avg_cluster_size, avg_bundle_pct, sample_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(funding_source) DO UPDATE SET
                   avg_first_buy_offset_s = excluded.avg_first_buy_offset_s,
                   avg_sniper_rate        = excluded.avg_sniper_rate,
                   avg_cluster_size       = excluded.avg_cluster_size,
                   avg_bundle_pct         = excluded.avg_bundle_pct,
                   sample_count           = sample_count + 1""",
            (str(uuid.uuid4()), funder, offset_s, is_sniper, cluster_size, supply_pct),
        )
    else:
        n = int(row["sample_count"] or 0)
        new_n = n + 1

        def rolling(old: float, new_val: float) -> float:
            return round((old * n + new_val) / new_n, 3)

        new_offset   = rolling(float(row["avg_first_buy_offset_s"] or 0), offset_s)
        new_sniper   = rolling(float(row["avg_sniper_rate"] or 0), is_sniper)
        new_size     = rolling(float(row["avg_cluster_size"] or 0), cluster_size)
        new_bundle   = rolling(float(row["avg_bundle_pct"] or 0), supply_pct)

        conn.execute(
            """UPDATE team_fingerprints
               SET avg_first_buy_offset_s = ?,
                   avg_sniper_rate        = ?,
                   avg_cluster_size       = ?,
                   avg_bundle_pct         = ?,
                   sample_count           = ?
               WHERE funding_source = ?""",
            (new_offset, new_sniper, new_size, new_bundle, new_n, funder),
        )

    conn.commit()


def update_fingerprint_outcome(token_mint: str, conn) -> None:
    """Update a funder fingerprint's outcome ledger (known_mints, outcome_labels,
    rug_rate, moon_rate, avg_bundle/dev_pct, keywords) after a 4h outcome.

    Owns the outcome-side columns of team_fingerprints; update_fingerprint owns
    the structural-average columns. Both upsert on funding_source so writer
    order doesn't matter (moved here from outcome_tracker so one module owns
    the table).
    """
    token_row = conn.execute(
        "SELECT bundle_pct, dev_pct, narrative_tags FROM tokens WHERE mint = ?",
        (token_mint,),
    ).fetchone()
    if not token_row:
        return

    funder_row = conn.execute(
        """SELECT w.funding_source
           FROM token_buyers tb
           JOIN wallets w ON w.address = tb.wallet_address
           WHERE tb.token_mint = ?
             AND w.funding_source IS NOT NULL
             AND w.funding_source != 'cex'
           GROUP BY w.funding_source
           ORDER BY COUNT(*) DESC LIMIT 1""",
        (token_mint,),
    ).fetchone()
    if not funder_row or not funder_row[0]:
        return
    funding_source = funder_row[0]

    outcome_row = conn.execute(
        "SELECT classified FROM coin_outcomes WHERE token_mint = ? AND check_offset_h = 4",
        (token_mint,),
    ).fetchone()
    outcome_label = outcome_row[0] if outcome_row else None

    bundle_pct = float(token_row["bundle_pct"] or 0)
    dev_pct = float(token_row["dev_pct"] or 0)
    new_keywords = json.loads(token_row["narrative_tags"] or "[]")
    now = int(time.time())

    fp_row = conn.execute(
        "SELECT * FROM team_fingerprints WHERE funding_source = ?", (funding_source,)
    ).fetchone()

    if fp_row is None:
        import uuid
        conn.execute(
            """INSERT INTO team_fingerprints
               (fingerprint_id, funding_source, known_mints, outcome_labels,
                avg_bundle_pct, avg_dev_pct, rug_rate, moon_rate,
                last_seen, description_keywords)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(funding_source) DO UPDATE SET
                   known_mints          = excluded.known_mints,
                   outcome_labels       = excluded.outcome_labels,
                   rug_rate             = excluded.rug_rate,
                   moon_rate            = excluded.moon_rate,
                   last_seen            = excluded.last_seen,
                   description_keywords = excluded.description_keywords""",
            (
                str(uuid.uuid4()), funding_source,
                json.dumps([token_mint]),
                json.dumps([outcome_label] if outcome_label else []),
                bundle_pct, dev_pct,
                1.0 if outcome_label == "rug" else 0.0,
                1.0 if outcome_label == "moon" else 0.0,
                now, json.dumps(new_keywords),
            ),
        )
    else:
        mints = json.loads(fp_row["known_mints"] or "[]")
        labels = json.loads(fp_row["outcome_labels"] or "[]")
        keywords = list(set(json.loads(fp_row["description_keywords"] or "[]") + new_keywords))
        if token_mint not in mints:
            mints.append(token_mint)
        if outcome_label:
            labels.append(outcome_label)
        n = len(labels) or 1
        prev_n = max(len(mints) - 1, 1)
        conn.execute(
            """UPDATE team_fingerprints SET
                   known_mints          = ?,
                   outcome_labels       = ?,
                   avg_bundle_pct       = ?,
                   avg_dev_pct          = ?,
                   rug_rate             = ?,
                   moon_rate            = ?,
                   last_seen            = ?,
                   description_keywords = ?
               WHERE funding_source = ?""",
            (
                json.dumps(mints), json.dumps(labels),
                round((float(fp_row["avg_bundle_pct"] or 0) * prev_n + bundle_pct) / len(mints), 3),
                round((float(fp_row["avg_dev_pct"] or 0) * prev_n + dev_pct) / len(mints), 3),
                labels.count("rug") / n, labels.count("moon") / n,
                now, json.dumps(keywords),
                funding_source,
            ),
        )
    conn.commit()


def compute_structural_distance(
    team_cluster: TeamCluster,
    conn,
) -> Optional[FingerprintMatch]:
    """Compare team_cluster structure against known rug fingerprints.

    Feature vector: [cluster_size/20, supply_pct/100, is_sniper, offset_s/300]
    Returns the closest match under the threshold, or None.
    """
    cluster_size = len(team_cluster.member_addresses)
    supply_pct   = float(team_cluster.supply_pct_at_graduation)
    is_sniper    = 1.0 if team_cluster.is_bc_sniper else 0.0
    offset_s     = float(team_cluster.first_buy_offset_seconds)

    new_vec = [
        cluster_size / _NORM_CLUSTER_SIZE,
        supply_pct   / _NORM_SUPPLY_PCT,
        is_sniper,
        offset_s     / _NORM_OFFSET_S,
    ]

    rows = conn.execute(
        """SELECT funding_source, avg_cluster_size, avg_bundle_pct,
                  avg_sniper_rate, avg_first_buy_offset_s, sample_count,
                  rug_rate
           FROM team_fingerprints
           WHERE rug_rate >= ? AND sample_count >= ?""",
        (_MIN_FINGERPRINT_RUG_RATE, _MIN_FINGERPRINT_SAMPLE),
    ).fetchall()

    if not rows:
        return None

    best: Optional[FingerprintMatch] = None
    best_dist = float("inf")

    for row in rows:
        known_vec = [
            float(row["avg_cluster_size"] or 0)       / _NORM_CLUSTER_SIZE,
            float(row["avg_bundle_pct"] or 0)          / _NORM_SUPPLY_PCT,
            float(row["avg_sniper_rate"] or 0),
            float(row["avg_first_buy_offset_s"] or 0)  / _NORM_OFFSET_S,
        ]
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(new_vec, known_vec)))

        if dist < best_dist:
            best_dist = dist
            best = FingerprintMatch(
                funding_source=row["funding_source"],
                distance=round(dist, 4),
                rug_rate=float(row["rug_rate"]),
                sample_count=int(row["sample_count"]),
            )

    if best and best.distance <= _FINGERPRINT_MATCH_DISTANCE:
        return best
    return None


# ── Assembly ──────────────────────────────────────────────────────────────────

def gather_memory_signals(
    team_cluster: Optional[TeamCluster],
    conn,
) -> MemorySignals:
    """Gather all memory signals for a graduation analysis in one call.

    Updates wallet graph and launch velocity as side effects.
    Returns a MemorySignals ready to pass into structural_read().
    """
    if team_cluster is None:
        return MemorySignals(
            graph_hits=[],
            fingerprint_match=None,
            launches_24h=0,
            launches_7d=0,
            expected_dump_start_h=None,
            dump_start_count=0,
        )

    members = team_cluster.member_addresses
    funder = team_cluster.funding_source

    # Layer 1: register cluster + query for external connections
    update_wallet_graph(members, outcome=None, conn=conn)
    graph_hits = query_wallet_graph(members, conn)

    # Layer 2: refresh and fetch velocity
    launches_24h, launches_7d = update_launch_velocity(funder, conn)

    # Layer 4: structural distance vs known rug fingerprints
    fingerprint_match = compute_structural_distance(team_cluster, conn)

    # Layer 3: fetch existing dump timing (no update here — updated at dist check)
    funder_row = None
    if funder and funder != "cex":
        funder_row = conn.execute(
            "SELECT avg_dump_start_h, dump_start_count FROM funder_reputation WHERE funding_source = ?",
            (funder,),
        ).fetchone()

    dump_start_count = int(funder_row["dump_start_count"] or 0) if funder_row else 0
    raw_avg = float(funder_row["avg_dump_start_h"]) if funder_row and funder_row["avg_dump_start_h"] else None
    expected_dump_h = raw_avg if dump_start_count >= _MIN_DUMP_START_OBSERVATIONS else None

    return MemorySignals(
        graph_hits=graph_hits,
        fingerprint_match=fingerprint_match,
        launches_24h=launches_24h,
        launches_7d=launches_7d,
        expected_dump_start_h=expected_dump_h,
        dump_start_count=dump_start_count,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _canonical_pairs(addresses: list[str]) -> list[tuple[str, str]]:
    """Return canonical (a < b) pairs for all unique combinations."""
    unique = list(set(addresses))
    pairs = []
    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            a, b = unique[i], unique[j]
            pairs.append((a, b) if a < b else (b, a))
    return pairs
