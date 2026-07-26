"""Train the discrete-time hazard model of team exit — the v5 architecture.

Design (validated walk-forward before this trainer existed; see eval/hazard.py):
  - person-period expansion of verified-clean coins (231 coins -> ~1,476 rows,
    168 exit events): uses duration + censoring instead of discarding them, which
    is why it reaches at n=231 what the binary heads needed ~1,800 rows for;
  - pooled logistic hazard (Singer-Willett), L2, median-impute + missing
    indicators + standardization — beats shallow GBM at this n (0.905 vs 0.894
    interval AUC) and is calibrated in-family (top decile pred 0.700 vs realized
    0.714, no Platt layer needed);
  - 14 features vs 168 events ≈ 12 events-per-variable (inside the 10-20 rule).

Measured (expanding walk-forward, folds BY COIN so clustered rows never straddle):
  interval AUC 0.905 · within-interval-0 AUC 0.852 (the pre-warning proper, no
  baseline-shape credit) · horizon exit<=10min AUC 0.750 · Brier 0.073 vs 0.116.

GATE: >=120 exit events AND >=150 coins, else no artifact (a hazard model's power
comes from events, not rows — the analogue of the binary heads' 500-row gate).

    uv run python scripts/train_hazard_model.py
"""

import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

MODEL_VERSION = "v5-hazard-pooled-logistic"
OUT = Path(__file__).parent.parent / "models" / "hazard_model_v5.pkl"
MIN_EVENTS = 120
MIN_COINS = 150


def main() -> None:
    from src.common.db import get_connection
    from src.analyzer.hazard_data import DEFAULT_EDGES, load_person_periods
    from eval.hazard import (STATIC_FEATURES, TV_FEATURES, build_matrix,
                             coin_folds, fit_hazard_logistic, roc_auc)

    conn = get_connection()
    rows, statics, grad_order = load_person_periods(conn)
    conn.close()
    X, y = build_matrix(rows, statics)
    n_events, n_coins = int(y.sum()), len(grad_order)
    print(f"coins={n_coins}  rows={len(rows)}  events={n_events}")
    if n_events < MIN_EVENTS or n_coins < MIN_COINS:
        print(f"GATE NOT MET (need >={MIN_EVENTS} events, >={MIN_COINS} coins) — no artifact")
        return

    # honest headline: walk-forward before the final full fit
    P, Y = [], []
    for tr, te in coin_folds(rows, grad_order):
        if y[tr].sum() < 15:
            continue
        P.append(fit_hazard_logistic(X[tr], y[tr])(X[te]))
        Y.append(y[te])
    p, yy = np.concatenate(P), np.concatenate(Y)
    wf_auc = roc_auc(p, yy)
    print(f"walk-forward interval AUC: {wf_auc:.3f}  "
          f"Brier {np.mean((p - yy) ** 2):.4f} vs base {np.mean((yy.mean() - yy) ** 2):.4f}")

    # final fit on everything — persist the raw sklearn model + preprocessing stats
    # (no closures pickled; inference re-applies the same transform)
    from sklearn.linear_model import LogisticRegression
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Ximp = np.where(np.isnan(X), med, X)
    mu, sd = Ximp.mean(axis=0), Ximp.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    A = np.hstack([(Ximp - mu) / sd, np.isnan(X).astype(float)])
    m = LogisticRegression(C=1.0, max_iter=2000)
    m.fit(A, y)

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "wb") as fh:
        pickle.dump({
            "version": MODEL_VERSION, "trained_at": int(time.time()),
            "edges": list(DEFAULT_EDGES),
            "static_features": list(STATIC_FEATURES), "tv_features": list(TV_FEATURES),
            "median": med, "mu": mu, "sd": sd, "sk_model": m,
            "n_coins": n_coins, "n_events": n_events, "wf_interval_auc": float(wf_auc),
            "note": "shadow only — calibrated per-interval exit hazards; alerts stay "
                    "on the rule engine until the audit's promotion gate passes",
        }, fh)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)  version={MODEL_VERSION}")


if __name__ == "__main__":
    main()
