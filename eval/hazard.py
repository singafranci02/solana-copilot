"""Walk-forward evaluation for discrete-time hazard models of team exit.

Folds are expanding and TIME-ORDERED BY COIN — all interval rows of one coin stay
on one side of every split (person-period rows are clustered within coin; letting
them straddle train/test would leak the coin's outcome into training). Metrics:

  - interval AUC: among coins at risk in interval i, does the model rank the ones
    that exit now above the ones that don't?
  - horizon AUC (the product metric): P(exit <= 10min | graduation state) from the
    composed hazards, scored against the realized label — directly comparable to
    the suspended binary team_exit10 head;
  - calibration: predicted vs realized hazard per interval (the alert threshold
    machinery depends on calibrated probabilities, not ranks).

    uv run python -m eval.hazard
"""

from __future__ import annotations

import sys

import numpy as np

from src.analyzer.hazard_data import DEFAULT_EDGES, load_person_periods

# A-priori static shortlist — chosen from the PREVIOUS population's model
# importances (out-of-sample w.r.t. current data) + theory, NOT selected on the
# data being evaluated (selection-on-test is a leak). Budget: 168 events / ~15
# features ≈ 11 events per variable.
STATIC_FEATURES = (
    "team_supply_pct",        # insider stake: more bag -> more to unload
    "unique_bc_buyers",       # crowd breadth (herding)
    "top3_holder_pct",        # concentration
    "team_score_mean",        # membership confidence
    "graph_rug_hits",         # operator recidivism
    "launches_24h",           # funder velocity (serial operators)
    "liquidity_usd_at_grad",  # exit capacity (can they get out?)
    "smart_money_count",      # informed presence
)
TV_FEATURES = ("tv_trades", "tv_wallets", "tv_price_vs_first", "tv_drawdown",
               "tv_net_flow_recent", "tv_log_t")


def build_matrix(rows, statics):
    X = np.empty((len(rows), len(STATIC_FEATURES) + len(TV_FEATURES)))
    for i, r in enumerate(rows):
        s = statics.get(r.token_mint, {})
        for j, k in enumerate(STATIC_FEATURES):
            v = s.get(k)
            X[i, j] = float(v) if v is not None else np.nan
        for j, k in enumerate(TV_FEATURES):
            X[i, len(STATIC_FEATURES) + j] = getattr(r, k)
    y = np.array([r.event for r in rows], dtype=float)
    return X, y


def coin_folds(rows, grad_order, n_folds: int = 5, min_train_coins: int = 60):
    """Expanding time-ordered folds over COINS (never over rows)."""
    order = {m: i for i, (m, _) in enumerate(grad_order)}
    coin_ids = np.array([order[r.token_mint] for r in rows])
    n_coins = len(grad_order)
    edges = np.linspace(int(n_coins * 0.4), n_coins, n_folds + 1).astype(int)
    for f in range(n_folds):
        a, b = edges[f], edges[f + 1]
        if a < min_train_coins or b - a < 10:
            continue
        yield (coin_ids < a), (coin_ids >= a) & (coin_ids < b)


def _prep(Xtr):
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    mu = np.nanmean(Xtr, axis=0)
    sd = np.nanstd(Xtr, axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)

    def f(X):
        M = np.isnan(X).astype(float)
        Z = (np.where(np.isnan(X), med, X) - mu) / sd
        return np.hstack([Z, M])
    return f


def fit_hazard_logistic(Xtr, ytr, l2: float = 1.0):
    """Pooled logistic hazard (Singer-Willett) with L2 — the small-n workhorse."""
    from sklearn.linear_model import LogisticRegression
    prep = _prep(Xtr)
    m = LogisticRegression(C=1.0 / l2, max_iter=2000)
    m.fit(prep(Xtr), ytr)
    return lambda X: m.predict_proba(prep(X))[:, 1]


def fit_hazard_gbm(Xtr, ytr):
    from sklearn.ensemble import GradientBoostingClassifier
    prep = _prep(Xtr)
    m = GradientBoostingClassifier(n_estimators=150, learning_rate=0.05, max_depth=2,
                                   subsample=0.8, min_samples_leaf=25, random_state=0)
    m.fit(prep(Xtr), ytr)
    return lambda X: m.predict_proba(prep(X))[:, 1]


def horizon_prob(pred, rows_coin, X_coin, horizon_s: int = 600):
    """P(exit <= horizon) from composed per-interval hazards: 1 - prod(1 - h_i)."""
    p = 1.0
    for r, x in zip(rows_coin, X_coin):
        if r.t_start >= horizon_s:
            break
        p *= 1.0 - float(pred(x[None, :])[0])
    return 1.0 - p


def roc_auc(scores, y):
    order = np.argsort(scores)
    ranks = np.empty(len(scores)); ranks[order] = np.arange(1, len(scores) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def main() -> None:
    from src.common.db import get_connection
    conn = get_connection()
    rows, statics, grad_order = load_person_periods(conn)
    conn.close()
    X, y = build_matrix(rows, statics)
    print(f"rows={len(rows)}  events={int(y.sum())}  coins={len(grad_order)}  "
          f"features={X.shape[1] // 1}")

    for name, fit in (("pooled-logistic L2", fit_hazard_logistic),
                      ("shallow GBM", fit_hazard_gbm)):
        # interval-level discrimination, walk-forward by coin
        P, Y = [], []
        hz_scores, hz_labels = [], []
        for tr, te in coin_folds(rows, grad_order):
            if y[tr].sum() < 15:
                continue
            pred = fit(X[tr], y[tr])
            P.append(pred(X[te])); Y.append(y[te])
            # horizon metric per test coin
            te_rows = [r for r, m in zip(rows, te) if m]
            te_X = X[te]
            by_coin: dict[str, list[int]] = {}
            for i, r in enumerate(te_rows):
                by_coin.setdefault(r.token_mint, []).append(i)
            for m_, idx in by_coin.items():
                rs = [te_rows[i] for i in idx]
                p10 = horizon_prob(pred, rs, te_X[idx], 600)
                # realized label: did the team exit within 10 min? (event in a row
                # with t_start < 600). Censored-before-600 coins excluded.
                exited = any(r.event and r.t_start < 600 for r in rs)
                observed = exited or any(r.t_end >= 600 for r in rs)
                if observed:
                    hz_scores.append(p10); hz_labels.append(float(exited))
        p, yy = np.concatenate(P), np.concatenate(Y)
        hs, hl = np.array(hz_scores), np.array(hz_labels)
        # calibration: predicted vs realized in deciles
        q = np.quantile(p, [0.5, 0.9])
        print(f"\n{name}:")
        print(f"  interval AUC (walk-forward, by-coin folds): {roc_auc(p, yy):.3f}  "
              f"(n={len(yy)}, events={int(yy.sum())})")
        print(f"  predicted hazard p50/p90: {q[0]:.3f}/{q[1]:.3f}   "
              f"realized event rate: {yy.mean():.3f}")
        print(f"  HORIZON exit<=10min AUC: {roc_auc(hs, hl):.3f}  "
              f"(n={len(hl)} coins, base {hl.mean():.1%})")


if __name__ == "__main__":
    sys.exit(main())
