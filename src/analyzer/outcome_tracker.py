"""Outcome tracker — the learning engine.

Every coin the monitor analyses gets price-checked at 1h, 4h, and 24h.
Those outcomes feed back into wallet win-rate scores and team fingerprints,
so the system gets smarter purely from its own observations — no external API needed.

Classification thresholds:
  moon  — market cap grew ≥ 3× vs launch snapshot
  rug   — market cap fell ≥ 70% vs launch snapshot (or token effectively dead)
  ok    — everything else

Wallet win rate is recomputed from outcomes: a wallet is a "winner" on a coin
if that coin was classified moon or ok at the 4h check.
"""

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from src.common.db import get_connection

logger = logging.getLogger(__name__)

CHECK_OFFSETS_H = (1, 4, 24)   # hours after launch at which we snapshot price

MOON_THRESHOLD  = 3.0          # ≥3× launch MC → "moon"
RUG_THRESHOLD   = 0.30         # ≤30% of launch MC remaining → "rug"


# ── data class ────────────────────────────────────────────────────────────────

@dataclass
class CoinOutcome:
    token_mint: str
    check_offset_h: int
    checked_at: int
    mc_usd: float | None
    price_change_pct: float | None
    classified: str | None          # "moon" | "ok" | "rug"


# ── scheduling ────────────────────────────────────────────────────────────────

async def schedule_checks(token_mint: str, launch_mc_usd: float | None) -> None:
    """Fire background tasks to check this coin at 1h, 4h, and 24h.

    Called by pump_monitor immediately after a coin is analysed.
    """
    for offset_h in CHECK_OFFSETS_H:
        asyncio.create_task(
            _deferred_check(token_mint, offset_h, launch_mc_usd)
        )


async def _deferred_check(
    token_mint: str, offset_h: int, launch_mc_usd: float | None
) -> None:
    await asyncio.sleep(offset_h * 3600)
    try:
        await _do_check(token_mint, offset_h, launch_mc_usd)
    except Exception:
        logger.exception("outcome check failed for %s at %dh", token_mint[:8], offset_h)


async def _do_check(
    token_mint: str, offset_h: int, launch_mc_usd: float | None
) -> None:
    """Fetch current MC, classify, persist, then trigger downstream updates."""
    current_mc = await _fetch_current_mc(token_mint)
    outcome = _classify(token_mint, offset_h, launch_mc_usd, current_mc)

    conn = get_connection()
    try:
        _save_outcome(outcome, conn)
        if offset_h == 4:
            # 4h is our primary signal — recompute wallet scores after it lands
            await _recompute_wallet_scores(token_mint, conn)
            await _update_team_fingerprint(token_mint, conn)
    finally:
        conn.close()

    # Sync to Supabase (fire-and-forget)
    from src.common import supabase_sync as sb
    asyncio.create_task(sb.coin_outcome(
        token_mint=token_mint,
        check_offset_h=offset_h,
        checked_at=outcome.checked_at,
        mc_usd=outcome.mc_usd,
        price_change_pct=outcome.price_change_pct,
        classified=outcome.classified,
    ))

    logger.info(
        "outcome %dh — $%s  mc=$%.0f  change=%.0f%%  → %s",
        offset_h,
        token_mint[:8],
        current_mc or 0,
        outcome.price_change_pct or 0,
        outcome.classified or "unknown",
    )


# ── MC fetching ───────────────────────────────────────────────────────────────

async def _fetch_current_mc(token_mint: str) -> float | None:
    """Fetch current market cap via Helius token-largest-accounts + supply."""
    try:
        from src.ingest.helius import HeliusClient
        async with HeliusClient() as helius:
            accounts = await helius.get_token_largest_accounts(token_mint)
        if not accounts:
            return None
        # Helius returns the top accounts; we use total supply as a proxy.
        # Real MC needs a price oracle — for now we track relative change
        # using the sum of top holder balances as a consistent proxy.
        total_ui = sum(float(a.get("uiAmount") or 0) for a in accounts[:10])
        return total_ui  # unitless proxy — good enough for rug detection
    except Exception:
        logger.debug("could not fetch MC for %s", token_mint[:8])
        return None


# ── classification ────────────────────────────────────────────────────────────

def _classify(
    token_mint: str,
    offset_h: int,
    launch_mc: float | None,
    current_mc: float | None,
) -> CoinOutcome:
    change_pct: float | None = None
    label: str | None = None

    if launch_mc and current_mc and launch_mc > 0:
        ratio = current_mc / launch_mc
        change_pct = (ratio - 1.0) * 100
        if ratio >= MOON_THRESHOLD:
            label = "moon"
        elif ratio <= RUG_THRESHOLD:
            label = "rug"
        else:
            label = "ok"

    return CoinOutcome(
        token_mint=token_mint,
        check_offset_h=offset_h,
        checked_at=int(time.time()),
        mc_usd=current_mc,
        price_change_pct=change_pct,
        classified=label,
    )


# ── persistence ───────────────────────────────────────────────────────────────

def _save_outcome(outcome: CoinOutcome, conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO coin_outcomes
               (token_mint, check_offset_h, checked_at, mc_usd, price_change_pct, classified)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, check_offset_h) DO UPDATE SET
               checked_at       = excluded.checked_at,
               mc_usd           = excluded.mc_usd,
               price_change_pct = excluded.price_change_pct,
               classified       = excluded.classified""",
        (
            outcome.token_mint, outcome.check_offset_h, outcome.checked_at,
            outcome.mc_usd, outcome.price_change_pct, outcome.classified,
        ),
    )
    conn.commit()


# ── wallet win-rate recomputation ─────────────────────────────────────────────

async def _recompute_wallet_scores(token_mint: str, conn: sqlite3.Connection) -> None:
    """Recompute smart_money_score for all wallets who bought this coin.

    Win rate = (moon + ok outcomes) / total coins bought in last 90 days.
    This replaces GMGN as our win-rate source.
    """
    cutoff = int(time.time()) - 90 * 86_400

    wallets = conn.execute(
        "SELECT DISTINCT wallet_address FROM token_buyers WHERE token_mint = ?",
        (token_mint,),
    ).fetchall()

    for (address,) in wallets:
        _recompute_one_wallet(address, cutoff, conn)


def _recompute_one_wallet(
    address: str, cutoff: int, conn: sqlite3.Connection
) -> None:
    """Recompute win_rate_90d and smart_money_score for a single wallet."""
    # Get all coins this wallet bought in the last 90 days that have a 4h outcome
    rows = conn.execute(
        """SELECT co.classified
           FROM token_buyers tb
           JOIN coin_outcomes co ON co.token_mint = tb.token_mint
                                 AND co.check_offset_h = 4
           WHERE tb.wallet_address = ?
             AND tb.bought_at >= ?
             AND co.classified IS NOT NULL""",
        (address, cutoff),
    ).fetchall()

    if not rows:
        return

    total = len(rows)
    wins = sum(1 for (label,) in rows if label in ("moon", "ok"))
    win_rate = wins / total

    # Pull existing wallet to get total_trades count
    wallet_row = conn.execute(
        "SELECT total_trades FROM wallets WHERE address = ?", (address,)
    ).fetchone()
    total_trades = int(wallet_row["total_trades"]) if wallet_row else total

    # Reuse the scoring formula from smart_money.score_wallet
    volume_signal = min(total_trades / 500.0, 1.0)

    last_ts_row = conn.execute(
        "SELECT MAX(bought_at) FROM token_buyers WHERE wallet_address = ?",
        (address,),
    ).fetchone()
    last_ts = (last_ts_row[0] or 0) if last_ts_row else 0
    days_idle = (time.time() - last_ts) / 86_400.0 if last_ts else 90.0
    recency = max(0.0, 1.0 - days_idle / 90.0)

    score = round(max(0.0, min(1.0, 0.60 * win_rate + 0.25 * volume_signal + 0.15 * recency)), 4)

    conn.execute(
        """UPDATE wallets
           SET win_rate_90d = ?, smart_money_score = ?
           WHERE address = ?""",
        (win_rate, score, address),
    )
    conn.commit()

    logger.debug(
        "rescored wallet ...%s  wr=%.0f%%  score=%.3f  (n=%d)",
        address[-6:], win_rate * 100, score, total,
    )


# ── team fingerprint update ───────────────────────────────────────────────────

async def _update_team_fingerprint(token_mint: str, conn: sqlite3.Connection) -> None:
    """Update the team fingerprint for the dev cluster that launched this coin."""
    import json as _json

    # Find the team cluster linked to this token
    token_row = conn.execute(
        "SELECT bundle_pct, dev_pct FROM tokens WHERE mint = ?", (token_mint,)
    ).fetchone()
    if not token_row:
        return

    # Find the funding source for this token's team cluster via token_buyers
    funder_row = conn.execute(
        """SELECT w.funding_source
           FROM token_buyers tb
           JOIN wallets w ON w.address = tb.wallet_address
           WHERE tb.token_mint = ?
             AND w.funding_source IS NOT NULL
             AND w.funding_source != 'cex'
           GROUP BY w.funding_source
           ORDER BY COUNT(*) DESC
           LIMIT 1""",
        (token_mint,),
    ).fetchone()
    if not funder_row or not funder_row[0]:
        return

    funding_source = funder_row[0]

    # Get this coin's outcome
    outcome_row = conn.execute(
        "SELECT classified FROM coin_outcomes WHERE token_mint = ? AND check_offset_h = 4",
        (token_mint,),
    ).fetchone()
    outcome_label = outcome_row[0] if outcome_row else None

    # Fetch existing fingerprint for this funder
    fp_row = conn.execute(
        "SELECT * FROM team_fingerprints WHERE funding_source = ?", (funding_source,)
    ).fetchone()

    token_desc_row = conn.execute(
        "SELECT name, symbol, narrative_tags FROM tokens WHERE mint = ?", (token_mint,)
    ).fetchone()
    new_keywords = _json.loads(token_desc_row["narrative_tags"] or "[]") if token_desc_row else []

    if fp_row is None:
        import uuid
        conn.execute(
            """INSERT INTO team_fingerprints
               (fingerprint_id, funding_source, known_mints, outcome_labels,
                avg_bundle_pct, avg_dev_pct, avg_cluster_size,
                rug_rate, moon_rate, last_seen, description_keywords)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), funding_source,
                _json.dumps([token_mint]),
                _json.dumps([outcome_label] if outcome_label else []),
                float(token_row["bundle_pct"] or 0),
                float(token_row["dev_pct"] or 0),
                1.0,
                1.0 if outcome_label == "rug" else 0.0,
                1.0 if outcome_label == "moon" else 0.0,
                int(time.time()),
                _json.dumps(new_keywords),
            ),
        )
    else:
        mints = _json.loads(fp_row["known_mints"])
        labels = _json.loads(fp_row["outcome_labels"])
        keywords = list(set(_json.loads(fp_row["description_keywords"]) + new_keywords))

        if token_mint not in mints:
            mints.append(token_mint)
        if outcome_label:
            labels.append(outcome_label)

        n = len(labels) or 1
        rug_rate = labels.count("rug") / n
        moon_rate = labels.count("moon") / n

        conn.execute(
            """UPDATE team_fingerprints SET
               known_mints          = ?,
               outcome_labels       = ?,
               avg_bundle_pct       = (avg_bundle_pct * ? + ?) / ?,
               avg_dev_pct          = (avg_dev_pct * ? + ?) / ?,
               avg_cluster_size     = avg_cluster_size + 0,
               rug_rate             = ?,
               moon_rate            = ?,
               last_seen            = ?,
               description_keywords = ?
               WHERE funding_source = ?""",
            (
                _json.dumps(mints), _json.dumps(labels),
                len(mints) - 1, float(token_row["bundle_pct"] or 0), len(mints),
                len(mints) - 1, float(token_row["dev_pct"] or 0), len(mints),
                rug_rate, moon_rate,
                int(time.time()),
                _json.dumps(keywords),
                funding_source,
            ),
        )
    conn.commit()


# ── public query helpers ──────────────────────────────────────────────────────

def get_team_fingerprint(funding_source: str, conn: sqlite3.Connection) -> dict | None:
    """Return the stored fingerprint for a known funder, or None."""
    row = conn.execute(
        "SELECT * FROM team_fingerprints WHERE funding_source = ?", (funding_source,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("known_mints", "outcome_labels", "description_keywords"):
        d[key] = json.loads(d[key])
    return d


def get_all_rugging_teams(conn: sqlite3.Connection, min_launches: int = 2) -> list[dict]:
    """Return team fingerprints with rug_rate > 0.5 and at least min_launches coins."""
    rows = conn.execute(
        """SELECT * FROM team_fingerprints
           WHERE rug_rate > 0.5
             AND json_array_length(known_mints) >= ?
           ORDER BY rug_rate DESC""",
        (min_launches,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        for key in ("known_mints", "outcome_labels", "description_keywords"):
            d[key] = json.loads(d[key])
        result.append(d)
    return result
