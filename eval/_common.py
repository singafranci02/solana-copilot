"""Shared harness core: point-in-time data loading, faithful rule replay, metrics.

The replay reconstructs the exact ctx structural_read saw at verdict time from
the frozen graduation_feature_snapshot, then calls the REAL structural_read — so
the harness auto-tracks any future rule change and never drifts from live logic.
Labels are read separately (post-verdict) and never enter the feature side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from src.common.db import get_connection
from src.strategy.rules import structural_read

HORIZONS = (1, 4, 24)
DISTRIBUTE_SIGNALS = {"DISTRIBUTING", "DUMPED"}


# ── records ───────────────────────────────────────────────────────────────────

@dataclass
class Sample:
    token_mint: str
    graduated_at: int
    features: dict
    stored_verdict: str | None
    stored_confidence: float | None
    # labels (post-verdict; None until the check has run)
    distribute: dict[int, bool | None] = field(default_factory=dict)   # by horizon
    outcome: dict[int, str | None] = field(default_factory=dict)       # moon/ok/rug by horizon
    mc_change_pct: dict[int, float | None] = field(default_factory=dict)


def load_samples(conn=None) -> list[Sample]:
    """Every pipeline-v2 snapshot joined to its graduation time + all labels."""
    own = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """SELECT gfs.token_mint, gfs.features_json, ge.graduated_at
               FROM graduation_feature_snapshot gfs
               JOIN graduation_events ge ON ge.token_mint = gfs.token_mint
               WHERE ge.pipeline_version >= 2
               ORDER BY ge.graduated_at""",
        ).fetchall()

        # bulk-load labels once
        dist = {}   # (mint, h) -> signal
        for r in conn.execute(
            "SELECT token_mint, check_offset_h, distribution_signal FROM post_grad_behavior"
        ):
            dist[(r["token_mint"], r["check_offset_h"])] = r["distribution_signal"]
        outc = {}   # (mint, h) -> (classified, change)
        for r in conn.execute(
            "SELECT token_mint, check_offset_h, classified, price_change_pct FROM coin_outcomes"
        ):
            outc[(r["token_mint"], r["check_offset_h"])] = (r["classified"], r["price_change_pct"])

        samples: list[Sample] = []
        for r in rows:
            mint = r["token_mint"]
            try:
                feats = json.loads(r["features_json"] or "{}")
            except Exception:
                continue
            s = Sample(
                token_mint=mint,
                graduated_at=int(r["graduated_at"]),
                features=feats,
                stored_verdict=feats.get("verdict"),
                stored_confidence=feats.get("confidence"),
            )
            for h in HORIZONS:
                sig = dist.get((mint, h))
                s.distribute[h] = (sig in DISTRIBUTE_SIGNALS) if sig is not None else None
                cl, ch = outc.get((mint, h), (None, None))
                s.outcome[h] = cl
                s.mc_change_pct[h] = ch
            samples.append(s)
        return samples
    finally:
        if own:
            conn.close()


# ── faithful ctx reconstruction + replay ──────────────────────────────────────

def ctx_from_features(f: dict) -> dict:
    """Rebuild the structural_read ctx from a frozen snapshot (stand-in objects).

    Graph hits collapse to ≤2 objects because the rules only test `any(...)`, not
    counts — reproducing rule behavior exactly without materializing 100k+ rows.
    """
    tsp = f.get("team_supply_pct")
    team_cluster = None
    if tsp is not None:
        team_cluster = SimpleNamespace(
            supply_pct_at_graduation=float(tsp),
            is_bc_sniper=bool(f.get("team_is_bc_sniper")),
        )

    funder_rep = None
    fn = int(f.get("funder_n") or 0)
    if fn > 0 and f.get("funder_rug_rate") is not None:
        rug = float(f["funder_rug_rate"])
        funder_rep = SimpleNamespace(
            graduated_mints=[None] * fn,
            rug_rate=rug,
            moon_rate=float(f.get("funder_moon_rate") or 0.0),
            is_known_rugger=(fn >= 8 and rug >= 0.65),
        )

    creator_rep = None
    cn = int(f.get("creator_n") or 0)
    if cn > 0 and f.get("creator_rug_rate") is not None:
        crug = float(f["creator_rug_rate"])
        creator_rep = {"n": cn, "rug_rate": crug,
                       "is_serial_rugger": (cn >= 8 and crug >= 0.65)}

    graph_hits = []
    n_rug = int(f.get("graph_rug_hits") or 0)
    n_all = int(f.get("graph_hits") or 0)
    if n_rug > 0:
        graph_hits.append(SimpleNamespace(
            connected_wallet="x" * 8, known_wallet="y" * 8,
            co_appearances=2, rug_co_appearances=2, last_seen_together=0))
    if n_all - n_rug > 0:
        graph_hits.append(SimpleNamespace(
            connected_wallet="a" * 8, known_wallet="b" * 8,
            co_appearances=2, rug_co_appearances=0, last_seen_together=0))
    fp = None
    if f.get("fingerprint_distance") is not None:
        fp = SimpleNamespace(distance=float(f["fingerprint_distance"]),
                             funding_source="f" * 8, rug_rate=0.7, sample_count=4)
    mem = SimpleNamespace(
        graph_hits=graph_hits, fingerprint_match=fp,
        launches_24h=int(f.get("launches_24h") or 0),
        launches_7d=int(f.get("launches_7d") or 0),
        expected_dump_start_h=f.get("expected_dump_start_h"),
        dump_start_count=(3 if f.get("expected_dump_start_h") is not None else 0),
    )

    return {
        "token_mint": "",
        "team_cluster": team_cluster,
        "funder_rep": funder_rep,
        "creator_rep": creator_rep,
        "smart_money_count": int(f.get("smart_money_count") or 0),
        "distribution_signal": None,                 # None at graduation, by design
        "bundle_pct": float(tsp) if tsp is not None else 0.0,
        "memory_signals": mem,
        "bc_duration_seconds": int(f.get("bc_duration_seconds") or -1),
        "top_holder_pct": float(f.get("top_holder_pct") or 0.0),
        "top3_holder_pct": float(f.get("top3_holder_pct") or 0.0),
        "unique_bc_buyers": int(f.get("unique_bc_buyers") or 0),
        "proven_wallet_count": int(f.get("proven_wallet_count") or 0),
        "launch_slot_snipe_count": int(f.get("launch_slot_snipe_count") or 0),
        "funder_leader_consistency": f.get("funder_leader_consistency"),
        "funder_choreography_n": f.get("funder_choreography_n"),
    }


def replay(f: dict):
    """Return (verdict, confidence) by running the real rule engine on a snapshot."""
    read = structural_read(ctx_from_features(f))
    return read.verdict, read.confidence


def distribute_score(verdict: str | None, confidence: float | None) -> float:
    """Continuous P(team distributes) proxy from the categorical verdict.

    SKIP = the rule predicts distribution (high), SOUND = predicts continuation
    (low). Used for PR-AUC / calibration of the rule baseline; a fitted model
    later emits this probability directly.
    """
    c = float(confidence or 0.5)
    if verdict == "SKIP":
        return 0.5 + 0.5 * c
    if verdict == "STRUCTURALLY_SOUND":
        return 0.5 - 0.5 * c
    return 0.5


# ── metrics (pure numpy — no sklearn) ──────────────────────────────────────────

def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    """PR-AUC via the average-precision estimator. labels ∈ {0,1}."""
    if labels.sum() == 0 or len(labels) == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    y = labels[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / y.sum()
    # integrate precision over recall increments
    rec_prev = np.concatenate([[0.0], recall[:-1]])
    return float(np.sum(precision * (recall - rec_prev)))


def brier(scores: np.ndarray, labels: np.ndarray) -> float:
    if len(labels) == 0:
        return float("nan")
    return float(np.mean((scores - labels) ** 2))


def prf1(pred: np.ndarray, labels: np.ndarray) -> tuple[float, float, float]:
    """precision, recall, F1 for a binary prediction against a binary label."""
    tp = float(np.sum((pred == 1) & (labels == 1)))
    fp = float(np.sum((pred == 1) & (labels == 0)))
    fn = float(np.sum((pred == 0) & (labels == 1)))
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if (p == p and r == r and p + r) else float("nan")
    return p, r, f


def calibration_bins(scores: np.ndarray, labels: np.ndarray, n_bins: int = 10):
    """Return (bin_mid, mean_pred, actual_rate, count) for a reliability curve."""
    out = []
    edges = np.linspace(0, 1, n_bins + 1)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        m = (scores >= lo) & (scores < hi if i < n_bins - 1 else scores <= hi)
        if m.sum() == 0:
            continue
        out.append(((lo + hi) / 2, float(scores[m].mean()),
                    float(labels[m].mean()), int(m.sum())))
    return out


def day_bucket(ts: int) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(ts))
