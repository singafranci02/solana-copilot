"""T+5min ATTENTION second-opinion — scored once the crowd is visible.

Distinct from model_verdict.py (the graduation-time shadow) in both moment and
input: this runs 5 minutes AFTER migration, off order flow rather than structure.
Given a coin still alive at minute 5, it calls survival to 60min at ROC 0.904 (the
top-5% survive 100% of the time) vs 0.806 from graduation structure alone.

It deliberately has NO pump head. See src/analyzer/early_attention.py — the 10x is
unpredictable from both structure (0.583) and early flow (0.592, once the price_run
label-leak is removed).

Fail-safe by construction: any missing artifact, missing dependency or error
returns None, and the pipeline carries on exactly as before. Analysis only — this
is an observation about crowd arrival, never a trade instruction.
"""

from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ARTIFACT = Path(__file__).parent.parent.parent / "models" / "early_model_v1.pkl"
_cache: dict | None = None
_load_failed = False


def _load() -> dict | None:
    global _cache, _load_failed
    if _cache is not None or _load_failed:
        return _cache
    try:
        with open(_ARTIFACT, "rb") as fh:
            _cache = pickle.load(fh)
        logger.info("early-attention model loaded: %s", _cache.get("version"))
        return _cache
    except Exception as exc:
        _load_failed = True
        logger.debug("early model unavailable (%s) — attention scoring disabled", exc)
        return None


def predict(features: dict) -> dict | None:
    """Return {'version', 'p_survive60'} from early-attention features."""
    art = _load()
    if not art:
        return None
    try:
        import numpy as np

        out: dict = {"version": art["version"]}
        for target, head in art["heads"].items():
            x = np.array([[float(features[k]) if features.get(k) is not None else np.nan
                           for k in head["keys"]]])
            med = head["median"]
            M = np.isnan(x).astype(float)
            A = np.hstack([np.where(np.isnan(x), med, x), M])
            p = float(head["sk_model"].predict_proba(A)[0, 1])
            w, b = head["platt"]
            pc = np.clip(p, 1e-6, 1 - 1e-6)
            z = np.log(pc / (1 - pc))
            out[f"p_{target}"] = round(float(1.0 / (1.0 + np.exp(-(float(w[0]) * z + b)))), 4)
        return out
    except Exception as exc:
        logger.debug("early prediction failed: %s", exc)
        return None


def upsert_early_prediction(conn, token_mint: str, window_s: int, pred: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO early_predictions
               (token_mint, model_version, predicted_at, window_s, p_moon10x, p_survive60)
           VALUES (?,?,?,?,?,?)""",
        (token_mint, pred.get("version"), int(time.time()), window_s,
         pred.get("p_moon10x"), pred.get("p_survive60")),
    )
