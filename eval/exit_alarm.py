"""The exit alarm's label, defined once so it cannot drift apart again.

WHY THIS FILE EXISTS. hazard_predictions.team_exited is not a prediction target.
landmark_row sets b_team_exited when a team sell occurred STRICTLY BEFORE the
checkpoint, and persist_landmark stores that. Verified 2026-08-18: of rows at
checkpoint 30 carrying team_exited=1, 70 of 70 have time_to_team_exit_s < 30. The
column records a PAST event.

p_exit, meanwhile, is model_a's hazard for the NEXT interval. Scoring the two
against each other measures detection of something already in the covariates, and
produced a headline "+29.7% lift, 95% CI [+15.9%, +43.6%]" that was reported as the
system's first established result. It was measuring the wrong quantity.

Corrected — at-risk rows only, against the interval the model actually predicts:

        ROC 0.568 | lift +2.1% | 95% CI [-14.3%, +19.6%] | P(no effect) 41.1%

model_a does not take b_team_exited as a feature, so this was never a
feature-is-label leak. It is a label-DEFINITION mismatch, which is exactly why the
single-feature leak canary could not see it: no individual feature was suspicious,
the target was. NEGATIVE_RESULTS #21.

THE CORRECT LABEL, used by both the audit gate and the pre-registered pooling test:
    at risk   := the team had NOT sold before the checkpoint
    positive  := the team's first sell falls in [checkpoint, next grid edge)
"""

from __future__ import annotations

import numpy as np


def next_edge(checkpoint_s: int) -> int | None:
    """The closing edge of the interval a checkpoint's hazard refers to."""
    from src.analyzer.hazard_data import GRID_EDGES
    if checkpoint_s not in GRID_EDGES or checkpoint_s == GRID_EDGES[-1]:
        return None
    return GRID_EDGES[GRID_EDGES.index(checkpoint_s) + 1]


def at_risk_rows(conn, platform: str, checkpoint_s: int):
    """(p_exit, label) over coins still at risk at the checkpoint.

    The label is recomputed from coin_trajectory rather than read from
    hazard_predictions.team_exited, which answers a different question.
    """
    b = next_edge(checkpoint_s)
    if b is None:
        return np.array([]), np.array([])
    rows = conn.execute(
        """SELECT h.p_exit AS p, ct.time_to_team_exit_s AS tx
           FROM hazard_predictions h
           JOIN coin_trajectory ct ON ct.token_mint = h.token_mint
           JOIN tokens t ON t.mint = h.token_mint
           WHERE h.checkpoint_s = ? AND t.platform = ? AND h.p_exit IS NOT NULL
             AND (ct.time_to_team_exit_s IS NULL
                  OR ct.time_to_team_exit_s >= ?)""",
        (checkpoint_s, platform, checkpoint_s)).fetchall()
    p = np.array([r["p"] for r in rows], dtype=float)
    tx = np.array([r["tx"] if r["tx"] is not None else np.inf for r in rows],
                  dtype=float)
    return p, ((tx >= checkpoint_s) & (tx < b)).astype(float)


def lift_with_ci(p: np.ndarray, y: np.ndarray, quantile: float = 0.80,
                 draws: int = 3000, seed: int = 0):
    """(lift, lo, hi, base_rate) or None when the sample cannot support a claim.

    Lift over the base rate, never absolute precision — a 94.2%-precision alarm was
    once 2.3 points WORSE than never alerting (NEGATIVE_RESULTS #18).
    """
    if len(y) < 20 or not 0 < y.mean() < 1:
        return None
    m = p >= np.quantile(p, quantile)
    if m.sum() < 5:
        return None
    lift = float(y[m].mean() - y.mean())
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(draws):
        i = rng.integers(0, len(y), len(y))
        pp, yy = p[i], y[i]
        mm = pp >= np.quantile(pp, quantile)
        if mm.sum() >= 5 and 0 < yy.mean() < 1:
            bs.append(yy[mm].mean() - yy.mean())
    if not bs:
        return None
    a = np.array(bs)
    return lift, float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5)), float(y.mean())
