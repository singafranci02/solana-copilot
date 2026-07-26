"""Train the v5 competing-risks hazard artifact (models A + B).

Design per the adversarially-reviewed spec (docs/V5_DESIGN.md):
  A — team-exit cause-specific hazard (collapse = competing event, off risk set)
  B — collapse hazard with team-exit state as TV covariates; at team_exited=0 it
      supplies h_D for the CIF forward recursion (shared-slopes assumption, one
      budgeted interaction race pending)
Pooled logistic (Singer-Willett), fixed L2=1.0 (tuned shrinkage is noise at this
EPV — Riley 2021), median impute + WHITELISTED missing indicators only.

Measured before this trainer existed (expanding walk-forward, folds BY COIN,
quarantined label-of-record population, T0 mode = TV at train medians):
  model A fold-averaged interval AUC 0.852
  T0 pre-warn CIF AUC: 5min 0.870 [0.801,0.933] · 10min 0.888 [0.808,0.954]

GATE (classic-only re-derivation): >=100 A-events AND >=150 coins. EPV printed
honestly; below-10 EPV is an open risk carried until data growth lifts it.

    uv run python scripts/train_hazard_model.py
"""

import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

MODEL_VERSION = "v5-competing-hazard-logistic"
OUT = Path(__file__).parent.parent / "models" / "hazard_model_v5.pkl"
MIN_EVENTS_A = 100
MIN_COINS = 150


def _fit_full(X, y, cols):
    from sklearn.linear_model import LogisticRegression
    from eval.hazard import INDICATOR_FEATURES
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Ximp = np.where(np.isnan(X), med, X)
    mu, sd = Ximp.mean(axis=0), Ximp.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    ind_idx = [i for i, c in enumerate(cols) if c in INDICATOR_FEATURES]
    A = np.hstack([(Ximp - mu) / sd, np.isnan(X[:, ind_idx]).astype(float)
                   if ind_idx else np.empty((len(X), 0))])
    m = LogisticRegression(C=1.0, max_iter=2000)
    m.fit(A, y)
    return {"sk_model": m, "median": med, "mu": mu, "sd": sd,
            "cols": list(cols), "ind_idx": ind_idx,
            "n_columns": len(cols) + len(ind_idx)}


def main() -> None:
    from src.common.db import get_connection
    from src.analyzer.hazard_data import GRID_EDGES, load_person_periods
    from eval.hazard import (B_EXTRA, build_matrix, coin_folds,
                             fit_hazard_logistic, roc_auc, train_tv_medians)

    conn = get_connection()
    A_rows, B_rows, statics, order = load_person_periods(conn)
    conn.close()
    Xa, ya, cols_a = build_matrix(A_rows, statics)
    Xb, yb, cols_b = build_matrix(B_rows, statics, B_EXTRA)
    ev_a, ev_b, n_coins = int(ya.sum()), int(yb.sum()), len(order)
    print(f"A: rows={len(A_rows)} events={ev_a}   B: rows={len(B_rows)} "
          f"events={ev_b}   coins={n_coins}")
    if ev_a < MIN_EVENTS_A or n_coins < MIN_COINS:
        print(f"GATE NOT MET (>= {MIN_EVENTS_A} A-events, >= {MIN_COINS} coins) — no artifact")
        return

    # honest walk-forward headline + fold-1 EPV disclosure
    fold_aucs = []
    first_epv = None
    for tr, te in coin_folds(A_rows, order):
        if ya[tr].sum() < 15:
            continue
        pred = fit_hazard_logistic(Xa[tr], ya[tr], cols_a)
        if first_epv is None:
            first_epv = float(ya[tr].sum()) / pred.n_columns
        a = roc_auc(pred(Xa[te]), ya[te])
        if not np.isnan(a):
            fold_aucs.append(a)
    fa = _fit_full(Xa, ya, cols_a)
    fb = _fit_full(Xb, yb, cols_b)
    epv_a = ev_a / fa["n_columns"]
    print(f"fold-avg interval AUC {np.mean(fold_aucs):.3f}   "
          f"EPV full {epv_a:.1f} / fold-1 {first_epv:.1f} "
          f"({fa['n_columns']} estimated columns)")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "wb") as fh:
        pickle.dump({
            "version": MODEL_VERSION, "trained_at": int(time.time()),
            "edges": list(GRID_EDGES),
            "model_a": fa, "model_b": fb,
            "tv_medians": train_tv_medians(A_rows),      # for T0-mode scoring
            "n_coins": n_coins, "n_events_a": ev_a, "n_events_b": ev_b,
            "wf_interval_auc": float(np.mean(fold_aucs)),
            "epv_a_full": float(epv_a), "epv_a_fold1": float(first_epv or 0),
            "note": "shadow only — T0 CIF pre-warn + landmark hazards; rule engine "
                    "stays in front until the promotion gates pass",
        }, fh)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)  version={MODEL_VERSION}")


if __name__ == "__main__":
    main()
