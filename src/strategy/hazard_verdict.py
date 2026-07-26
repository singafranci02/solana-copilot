"""v5 hazard SHADOW scorer — T0 pre-warn CIFs + landmark next-interval hazards.

Distinct from model_verdict.py (the v4 binary shadow) in what it emits: calibrated
per-interval hazards from the competing-risks pair (model A team-exit, model B
collapse), composed per docs/V5_DESIGN.md. Every probability is tagged with the
scoring MODE that produced it — track-record claims must cite the matching mode:

  T0        graduation-time CIF_A(h): TV covariates fixed at the artifact's stored
            train-set per-interval medians (never realized values — the T+0 alert
            cannot know them);
  LANDMARK  checkpoint re-score: next-interval h_A (exit) and h_B (collapse) from
            covariates strictly before the checkpoint edge.

Fail-safe by construction: missing artifact / any error -> None, pipeline
unaffected. SHADOW ONLY — nothing here drives alerts until the promotion gates.
"""

from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ARTIFACT = Path(__file__).parent.parent.parent / "models" / "hazard_model_v5.pkl"
_cache: dict | None = None
_load_failed = False

T0_HORIZONS_S = (300, 600, 1200, 2400, 3600)


def _load() -> dict | None:
    global _cache, _load_failed
    if _cache is not None or _load_failed:
        return _cache
    try:
        with open(_ARTIFACT, "rb") as fh:
            _cache = pickle.load(fh)
        logger.info("hazard shadow loaded: %s (A events=%s, wf AUC=%.3f)",
                    _cache.get("version"), _cache.get("n_events_a"),
                    _cache.get("wf_interval_auc", 0.0))
        return _cache
    except Exception as exc:
        _load_failed = True
        logger.debug("hazard artifact unavailable (%s) — v5 shadow disabled", exc)
        return None


def _predict_one(model: dict, values: dict) -> float:
    """Apply one stored model to a feature dict. Replicates the training transform
    exactly: median impute, standardize, missing indicators for ind_idx columns."""
    import numpy as np
    cols = model["cols"]
    x = np.array([[float(values[c]) if values.get(c) is not None else np.nan
                   for c in cols]])
    miss = np.isnan(x[:, model["ind_idx"]]).astype(float) if model["ind_idx"] \
        else np.empty((1, 0))
    z = (np.where(np.isnan(x), model["median"], x) - model["mu"]) / model["sd"]
    A = np.hstack([z, miss])
    return float(model["sk_model"].predict_proba(A)[0, 1])


def score_t0(statics: dict) -> dict[int, float] | None:
    """Graduation-time pre-warn: {horizon_s: CIF_A(horizon)} via the competing-risks
    forward recursion, h_D = model B at team_exited=0, TV at stored train medians."""
    art = _load()
    if not art:
        return None
    try:
        edges = art["edges"]
        tv_med = art["tv_medians"]
        out: dict[int, float] = {}
        for h in T0_HORIZONS_S:
            surv, cif = 1.0, 0.0
            for i, (a, b) in enumerate(zip(edges, edges[1:])):
                if a >= h:
                    break
                med = tv_med.get(i) or tv_med.get(str(i)) or {}
                vals = dict(statics)
                vals.update(med)
                vals.setdefault("b_team_exited", 0.0)
                vals.setdefault("b_log_t_since_exit", 0.0)
                vals.setdefault("b_team_sellers", 0.0)
                vals.setdefault("b_log_prints", med.get("b_log_prints", 0.0))
                ha = _predict_one(art["model_a"], vals)
                hd = _predict_one(art["model_b"], vals)
                tot = min(ha + hd, 0.999)
                if ha + hd > 0.999:
                    ha *= tot / (ha + hd)
                cif += surv * ha
                surv *= max(1.0 - tot, 1e-9)
            out[h] = round(cif, 4)
        return out
    except Exception as exc:
        logger.debug("T0 hazard scoring failed: %s", exc)
        return None


def score_landmark(statics: dict, pp) -> tuple[float, float] | None:
    """Checkpoint re-score from a hazard_data.landmark_row: (h_A next-interval,
    h_B next-interval). h_A is meaningful only while the team has not exited;
    the caller stores b_team_exited alongside so grading can respect that."""
    art = _load()
    if not art:
        return None
    try:
        vals = dict(statics)
        for k in ("tv_trades", "tv_wallets", "tv_price_vs_first", "tv_drawdown",
                  "tv_net_flow_recent", "tv_log_t", "b_team_exited",
                  "b_log_t_since_exit", "b_team_sellers", "b_log_prints"):
            vals[k] = getattr(pp, k)
        ha = _predict_one(art["model_a"], vals)
        hb = _predict_one(art["model_b"], vals)
        return round(ha, 4), round(hb, 4)
    except Exception as exc:
        logger.debug("landmark hazard scoring failed: %s", exc)
        return None


def persist_t0(conn, token_mint: str, cifs: dict[int, float]) -> None:
    art = _load()
    now = int(time.time())
    conn.executemany(
        """INSERT OR REPLACE INTO hazard_predictions
               (token_mint, scored_at, mode, checkpoint_s, horizon_s, p_exit,
                p_collapse, team_exited, model_version)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(token_mint, now, "T0", 0, h, p, None, 0, art.get("version") if art else None)
         for h, p in cifs.items()])
    conn.commit()


def persist_landmark(conn, token_mint: str, checkpoint_s: int,
                     ha: float, hb: float, team_exited: bool) -> None:
    art = _load()
    conn.execute(
        """INSERT OR REPLACE INTO hazard_predictions
               (token_mint, scored_at, mode, checkpoint_s, horizon_s, p_exit,
                p_collapse, team_exited, model_version)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (token_mint, int(time.time()), "LANDMARK", checkpoint_s, 0, ha, hb,
         int(team_exited), art.get("version") if art else None))
    conn.commit()
