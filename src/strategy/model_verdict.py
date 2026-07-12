"""Fitted-model SECOND OPINION (shadow) — never overrides the live verdict.

The live verdict remains `verdict_rules_v2` (src/strategy/rules.py). This module
loads the trained artifact and records what the model WOULD have said, so both
can be compared on live data before any promotion. Per docs/RESEARCH_PLAN.md:

  - hard-SKIP rules stay IN FRONT of any model (near-deterministic on-chain facts,
    not probabilities — they must not be softened),
  - the model replaces only the soft factor score, and only once promoted,
  - no silent promotions: eval/drift.py's gate must PASS first.

Fail-safe by construction: a missing artifact, a missing dependency, or any error
returns None and the pipeline is unaffected.

Validated (walk-forward, out-of-time): p_distribute ROC-AUC 0.921,
p_rug ROC-AUC 0.859 — vs 0.580 / 0.575 for the ruleset (eval/MODEL_BASELINE.md).
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path

logger = logging.getLogger(__name__)

_ARTIFACT = Path(__file__).parent.parent.parent / "models" / "verdict_model_v3.pkl"
_cache: dict | None = None
_load_failed = False


def _load() -> dict | None:
    """Lazy-load the artifact once. Never raises."""
    global _cache, _load_failed
    if _cache is not None or _load_failed:
        return _cache
    try:
        with open(_ARTIFACT, "rb") as fh:
            _cache = pickle.load(fh)
        logger.info("model second-opinion loaded: %s (trained %s)",
                    _cache.get("version"), _cache.get("trained_at"))
        return _cache
    except Exception as exc:
        _load_failed = True     # don't retry every graduation
        logger.debug("model artifact unavailable (%s) — shadow predictions disabled", exc)
        return None


def predict(features: dict) -> dict | None:
    """Return {'version', 'p_distribute', 'p_rug'} for a snapshot feature dict.

    Returns None if the artifact is unavailable or anything goes wrong — the
    caller must treat this as "no second opinion", never as a failure.
    """
    art = _load()
    if not art:
        return None
    try:
        import numpy as np

        out: dict = {"version": art["version"]}
        for target, head in art["heads"].items():
            keys = head["keys"]
            x = np.array([[float(features[k]) if features.get(k) is not None else np.nan
                           for k in keys]])
            med = head["median"]
            M = np.isnan(x).astype(float)
            A = np.hstack([np.where(np.isnan(x), med, x), M])
            p = float(head["sk_model"].predict_proba(A)[0, 1])
            # Platt scaling (same transform as training)
            w, b = head["platt"]
            z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
            p_cal = float(1.0 / (1.0 + np.exp(-(float(w[0]) * z + b))))
            out[f"p_{target}"] = round(p_cal, 4)
        return out
    except Exception as exc:
        logger.debug("model prediction failed: %s", exc)
        return None


def upsert_prediction(conn, token_mint: str, pred: dict, rule_verdict: str | None) -> None:
    """Persist the shadow prediction beside the live rule verdict for comparison."""
    import time
    conn.execute(
        """INSERT OR REPLACE INTO model_predictions
               (token_mint, model_version, p_distribute, p_rug, rule_verdict, predicted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_mint, pred.get("version"), pred.get("p_distribute"), pred.get("p_rug"),
         rule_verdict, int(time.time())),
    )
    conn.commit()
