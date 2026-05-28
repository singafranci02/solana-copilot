"""Pattern query helpers — correlations from accumulated graduation data.

Every result carries sample_size and is_significant.
is_significant is True only when n >= MIN_SIGNIFICANT_N (30 per bucket).

Patterns below threshold are flagged as hypothesis-level and must NOT
feed automated warnings or structural reads — they are informational only.
"""

import sqlite3
from dataclasses import dataclass, field

MIN_SIGNIFICANT_N = 30   # minimum samples per bucket for significance


@dataclass
class PatternResult:
    label: str
    sample_size: int
    value: float          # the metric (rate, pct, avg) being reported
    is_significant: bool  # True only when sample_size >= MIN_SIGNIFICANT_N
    note: str = ""        # "hypothesis, insufficient data" when not significant


def _result(label: str, sample_size: int, value: float, note: str = "") -> PatternResult:
    significant = sample_size >= MIN_SIGNIFICANT_N
    if not significant and not note:
        note = "hypothesis, insufficient data"
    return PatternResult(
        label=label,
        sample_size=sample_size,
        value=round(value, 4),
        is_significant=significant,
        note=note,
    )


# ── Core pattern queries ───────────────────────────────────────────────────────

def rug_rate_by_team_supply_pct(
    conn: sqlite3.Connection,
    buckets: list[tuple[float, float]] | None = None,
) -> list[PatternResult]:
    """Does higher team supply at graduation predict rugs?

    Buckets the data by supply_pct_at_graduation ranges and computes rug_rate
    per bucket from 4h coin_outcomes.

    Args:
        conn: SQLite connection.
        buckets: List of (low, high) pct ranges. Defaults to five 20-pct bands.
    """
    if buckets is None:
        buckets = [(0, 20), (20, 35), (35, 50), (50, 70), (70, 100)]

    results: list[PatternResult] = []
    for lo, hi in buckets:
        row = conn.execute(
            """SELECT
                   COUNT(*) as n,
                   SUM(CASE WHEN co.classified = 'rug' THEN 1 ELSE 0 END) as rugs
               FROM team_clusters tc
               JOIN coin_outcomes co ON co.token_mint = tc.token_mint
                                     AND co.check_offset_h = 4
               WHERE tc.supply_pct_at_graduation >= ? AND tc.supply_pct_at_graduation < ?
                 AND co.classified IS NOT NULL""",
            (lo, hi),
        ).fetchone()
        n = int(row["n"] or 0)
        rugs = int(row["rugs"] or 0)
        rate = rugs / n if n > 0 else 0.0
        results.append(_result(
            label=f"rug_rate supply_pct {lo}-{hi}%",
            sample_size=n,
            value=rate,
        ))
    return results


def rug_rate_by_sniper_flag(conn: sqlite3.Connection) -> list[PatternResult]:
    """Do BC snipers (bought within first 30s) rug at higher rates?"""
    results: list[PatternResult] = []
    for sniper_val, label in ((1, "is_bc_sniper"), (0, "not_bc_sniper")):
        row = conn.execute(
            """SELECT
                   COUNT(*) as n,
                   SUM(CASE WHEN co.classified = 'rug' THEN 1 ELSE 0 END) as rugs
               FROM team_clusters tc
               JOIN coin_outcomes co ON co.token_mint = tc.token_mint
                                     AND co.check_offset_h = 4
               WHERE tc.is_bc_sniper = ?
                 AND co.classified IS NOT NULL""",
            (sniper_val,),
        ).fetchone()
        n = int(row["n"] or 0)
        rugs = int(row["rugs"] or 0)
        rate = rugs / n if n > 0 else 0.0
        results.append(_result(label=f"rug_rate {label}", sample_size=n, value=rate))
    return results


def moon_rate_by_smart_money_count(
    conn: sqlite3.Connection,
    thresholds: list[int] | None = None,
) -> list[PatternResult]:
    """Does smart money presence predict moon outcomes?

    Groups graduated tokens by how many smart money wallets bought and reports
    moon rate (moon / total outcomes) at 4h.
    """
    if thresholds is None:
        thresholds = [0, 1, 2, 3]   # buckets: 0, 1, 2, 3+

    results: list[PatternResult] = []
    for i, lo in enumerate(thresholds):
        hi = thresholds[i + 1] if i + 1 < len(thresholds) else None
        label = f"sm_count={lo}" if hi is None else f"sm_count={lo}-{hi-1}"
        having_clause = f">= {lo}" if hi is None else f"BETWEEN {lo} AND {hi - 1}"

        row = conn.execute(
            f"""SELECT
                   COUNT(*) as n,
                   SUM(CASE WHEN co.classified = 'moon' THEN 1 ELSE 0 END) as moons
               FROM (
                   SELECT tb.token_mint, COUNT(DISTINCT tb.wallet_address) as sm_count
                   FROM token_buyers tb
                   JOIN wallets w ON w.address = tb.wallet_address
                   WHERE w.smart_money_score >= 0.7
                   GROUP BY tb.token_mint
               ) sub
               JOIN coin_outcomes co ON co.token_mint = sub.token_mint
                                     AND co.check_offset_h = 4
               WHERE sub.sm_count {having_clause}
                 AND co.classified IS NOT NULL""",
        ).fetchone()
        n = int(row["n"] or 0)
        moons = int(row["moons"] or 0)
        rate = moons / n if n > 0 else 0.0
        results.append(_result(label=f"moon_rate {label}", sample_size=n, value=rate))
    return results


def distribution_signal_vs_outcome(conn: sqlite3.Connection) -> list[PatternResult]:
    """Does the 1h distribution signal predict 4h outcome?"""
    signals = ("ACCUMULATING", "HOLDING", "DISTRIBUTING", "DUMPED")
    results: list[PatternResult] = []
    for signal in signals:
        row = conn.execute(
            """SELECT
                   COUNT(*) as n,
                   SUM(CASE WHEN co.classified IN ('moon','ok') THEN 1 ELSE 0 END) as good
               FROM post_grad_behavior pgb
               JOIN coin_outcomes co ON co.token_mint = pgb.token_mint
                                     AND co.check_offset_h = 4
               WHERE pgb.check_offset_h = 1
                 AND pgb.distribution_signal = ?
                 AND co.classified IS NOT NULL""",
            (signal,),
        ).fetchone()
        n = int(row["n"] or 0)
        good = int(row["good"] or 0)
        rate = good / n if n > 0 else 0.0
        results.append(_result(
            label=f"positive_outcome_rate signal={signal}",
            sample_size=n,
            value=rate,
        ))
    return results


def funder_rug_rate_distribution(conn: sqlite3.Connection) -> list[PatternResult]:
    """Distribution of funder rug rates across all tracked funders.

    Returns a summary of how many funders fall into each rug-rate bucket,
    only counting funders with >= MIN_SIGNIFICANT_N (30) graduated mints.
    """
    rows = conn.execute(
        """SELECT funding_source, rug_rate,
                  json_array_length(graduated_mints) as n_mints
           FROM funder_reputation
           WHERE json_array_length(graduated_mints) >= ?""",
        (MIN_SIGNIFICANT_N,),
    ).fetchall()

    total = len(rows)
    high_rug = sum(1 for r in rows if float(r["rug_rate"]) >= 0.65)
    mid_rug  = sum(1 for r in rows if 0.35 <= float(r["rug_rate"]) < 0.65)
    low_rug  = sum(1 for r in rows if float(r["rug_rate"]) < 0.35)

    return [
        _result("funders high_rug (>=65%)", total, high_rug / total if total else 0.0),
        _result("funders mid_rug (35-65%)", total, mid_rug / total if total else 0.0),
        _result("funders low_rug (<35%)",   total, low_rug / total if total else 0.0),
    ]


def avg_detection_lag_seconds(conn: sqlite3.Connection) -> PatternResult:
    """Average lag between graduation event and our detection."""
    row = conn.execute(
        "SELECT COUNT(*) as n, AVG(detection_lag_seconds) as avg_lag FROM graduation_events"
    ).fetchone()
    n = int(row["n"] or 0)
    avg = float(row["avg_lag"] or 0.0)
    return _result("avg_detection_lag_seconds", n, avg)


def all_patterns(conn: sqlite3.Connection) -> dict[str, list[PatternResult]]:
    """Run all pattern queries and return them keyed by topic."""
    return {
        "rug_rate_by_team_supply_pct": rug_rate_by_team_supply_pct(conn),
        "rug_rate_by_sniper_flag":     rug_rate_by_sniper_flag(conn),
        "moon_rate_by_smart_money":    moon_rate_by_smart_money_count(conn),
        "distribution_signal_vs_outcome": distribution_signal_vs_outcome(conn),
        "funder_rug_rate_distribution":   funder_rug_rate_distribution(conn),
        "detection_lag":               [avg_detection_lag_seconds(conn)],
    }
