"""Train the production model artifact (GBM + Platt) from labeled v2 snapshots.

Produces models/verdict_model_v3.pkl with BOTH heads:
  - p_distribute : will the team distribute within 4h  (the structural product)
  - p_rug        : will the coin lose >50% of MC within 4h (the money outcome)

Validated in eval/MODEL_BASELINE.md (expanding walk-forward, out-of-time):
distribute ROC-AUC 0.921, rug ROC-AUC 0.859 — vs 0.580 / 0.575 for the ruleset.

The artifact stores the exact feature list, imputation medians and Platt params,
so inference is reproducible and version-pinned (no closures pickled). Re-run on
a cadence; eval/drift.py's promotion gate must PASS before a new artifact is
trusted.

    uv run python scripts/train_model.py
"""

import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from eval._common import load_samples
from eval.model import feature_names, build_matrix_nan, platt_fit, _label, roc_auc

MODEL_VERSION = "v4-trajectory-gbm-platt"
OUT = Path(__file__).parent.parent / "models" / "verdict_model_v4.pkl"
HORIZON = 4


def _prep(X: np.ndarray, med: np.ndarray) -> np.ndarray:
    """Median-impute + missing-indicators. MUST match src/strategy/model_verdict."""
    M = np.isnan(X).astype(float)
    return np.hstack([np.where(np.isnan(X), med, X), M])


def _fit_gbm(A: np.ndarray, y: np.ndarray):
    from sklearn.ensemble import GradientBoostingClassifier
    m = GradientBoostingClassifier(
        n_estimators=300, learning_rate=0.05, max_depth=3,
        subsample=0.8, min_samples_leaf=30, random_state=0)
    m.fit(A, y)
    return m


def train_head(samples, target: str) -> dict:
    ss = [s for s in samples if _label(s, HORIZON, target) is not None]
    ss.sort(key=lambda s: s.graduated_at)
    keys = feature_names(ss, set())
    X = build_matrix_nan(ss, keys)
    y = np.array([_label(s, HORIZON, target) for s in ss])

    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)

    # Platt calibrator fit on a held-back TIME tail of train (never on the future)
    icut = int(len(X) * 0.8)
    inner = _fit_gbm(_prep(X[:icut], med), y[:icut])
    p_tail = inner.predict_proba(_prep(X[icut:], med))[:, 1]
    platt = platt_fit(p_tail, y[icut:])
    tail_roc = roc_auc(p_tail, y[icut:])

    model = _fit_gbm(_prep(X, med), y)      # final fit on everything
    print(f"  {target:<11} n={len(y):<5} base={y.mean():5.1%}  held-back-tail ROC={tail_roc:.3f}")
    return {
        "keys": keys, "median": med, "sk_model": model, "platt": platt,
        "n": int(len(y)), "base_rate": float(y.mean()), "tail_roc": float(tail_roc),
    }


def main() -> None:
    samples = load_samples()
    print(f"training on {len(samples)} pipeline-v2 snapshots")
    heads = {t: train_head(samples, t) for t in
             ("survive60", "team_exit10", "moon10x", "fastrug", "distribute", "rug")}

    OUT.parent.mkdir(exist_ok=True)
    artifact = {
        "version": MODEL_VERSION,
        "trained_at": int(time.time()),
        "horizon_h": HORIZON,
        "heads": heads,
        "note": "shadow / second-opinion only — hard-SKIP rules stay in front",
    }
    with open(OUT, "wb") as fh:
        pickle.dump(artifact, fh)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)  version={MODEL_VERSION}")


if __name__ == "__main__":
    main()
