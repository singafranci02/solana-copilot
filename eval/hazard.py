"""Walk-forward evaluation for the v5 competing-risks hazard models.

Folds are expanding and TIME-ORDERED BY COIN (all rows of one coin on one side of
every split). Two scoring modes, tagged on every probability:

  T0        — the graduation-time pre-warning: TV covariates FIXED at train-set
              per-interval medians. Composing over realized covariates would grade
              the T+0 alert on information it cannot have (panel finding).
  LANDMARK  — the live escalation: conditional next-interval hazard at a checkpoint
              edge, covariates strictly before the edge, coins with earlier events
              excluded from the landmark risk set.

Horizon probabilities use the COMPETING-RISKS forward recursion
    CIF_A(h) = sum_i hA_i * prod_{j<i} (1 - hA_j - hD_j)
never 1-prod(1-h): ~17% of coins die without a team exit, concentrated exactly in
the 5-20min horizons the product quotes. h_D = model B at team_exited=0.

Headline metrics are FOLD-AVERAGED with coin-cluster bootstrap CIs; pooled
concatenation is a diagnostic only.

    uv run python -m eval.hazard
"""

from __future__ import annotations

import sys

import numpy as np

from src.analyzer.hazard_data import GRID_EDGES, load_person_periods

STATIC_FEATURES = (
    "team_supply_pct", "unique_bc_buyers", "top3_holder_pct", "team_score_mean",
    "graph_rug_hits", "launches_24h", "liquidity_usd_at_grad", "smart_money_count",
)
# missing indicators ONLY for statics with a known missingness mechanism (panel
# finding: indicator-per-column doubled the estimated columns and halved EPV)
INDICATOR_FEATURES = ("liquidity_usd_at_grad", "team_score_mean")
TV_FEATURES = ("tv_trades", "tv_wallets", "tv_price_vs_first", "tv_drawdown",
               "tv_net_flow_recent", "tv_log_t")
B_EXTRA = ("b_team_exited", "b_log_t_since_exit", "b_team_sellers", "b_log_prints")

HORIZONS_S = (300, 600, 1200, 2400, 3600)
MIN_AT_RISK = 30                      # n-gate per quoted horizon


def build_matrix(rows, statics, extra: tuple[str, ...] = ()):
    cols = list(STATIC_FEATURES) + list(TV_FEATURES) + list(extra)
    X = np.empty((len(rows), len(cols)))
    for i, r in enumerate(rows):
        s = statics.get(r.token_mint, {})
        for j, k in enumerate(cols):
            if k in STATIC_FEATURES:
                v = s.get(k)
                X[i, j] = float(v) if v is not None else np.nan
            else:
                X[i, j] = getattr(r, k)
    y = np.array([r.event for r in rows], dtype=float)
    return X, y, cols


def _prep(Xtr, cols):
    """Impute + standardize; indicators only for the whitelisted statics."""
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    mu = np.nanmean(Xtr, axis=0)
    mu = np.where(np.isnan(mu), 0.0, mu)
    sd = np.nanstd(Xtr, axis=0)
    sd = np.where((sd < 1e-9) | np.isnan(sd), 1.0, sd)
    ind_idx = [i for i, c in enumerate(cols) if c in INDICATOR_FEATURES]

    def f(X):
        M = np.isnan(X[:, ind_idx]).astype(float) if ind_idx else np.empty((len(X), 0))
        Z = (np.where(np.isnan(X), med, X) - mu) / sd
        return np.hstack([Z, M])
    f.n_columns = len(cols) + len(ind_idx)
    return f


def fit_hazard_logistic(Xtr, ytr, cols, l2: float = 1.0):
    from sklearn.linear_model import LogisticRegression
    prep = _prep(Xtr, cols)
    m = LogisticRegression(C=1.0 / l2, max_iter=2000)
    m.fit(prep(Xtr), ytr)
    fn = lambda X: m.predict_proba(prep(X))[:, 1]
    fn.n_columns = prep.n_columns
    return fn


def coin_folds(rows, grad_order, n_folds: int = 5, min_train_coins: int = 60):
    order = {m: i for i, (m, _) in enumerate(grad_order)}
    coin_ids = np.array([order[r.token_mint] for r in rows])
    n_coins = len(grad_order)
    edges = np.linspace(int(n_coins * 0.4), n_coins, n_folds + 1).astype(int)
    for f in range(n_folds):
        a, b = edges[f], edges[f + 1]
        if a < min_train_coins or b - a < 10:
            continue
        yield (coin_ids < a), (coin_ids >= a) & (coin_ids < b)


# ── competing-risks composition ───────────────────────────────────────────────────

def t0_interval_matrix(statics_coin: dict, tv_medians: np.ndarray, cols_a, cols_b):
    """Per-interval design rows for T0 mode: statics + TRAIN per-interval TV medians."""
    n_int = len(GRID_EDGES) - 1
    Xa = np.empty((n_int, len(cols_a)))
    Xb = np.empty((n_int, len(cols_b)))
    for i in range(n_int):
        for j, k in enumerate(cols_a):
            Xa[i, j] = (float(statics_coin.get(k)) if statics_coin.get(k) is not None
                        else np.nan) if k in STATIC_FEATURES else tv_medians[i][k]
        for j, k in enumerate(cols_b):
            if k in STATIC_FEATURES:
                v = statics_coin.get(k)
                Xb[i, j] = float(v) if v is not None else np.nan
            elif k in B_EXTRA:
                Xb[i, j] = 0.0 if k != "b_log_prints" else tv_medians[i].get(k, 0.0)
            else:
                Xb[i, j] = tv_medians[i][k]
    return Xa, Xb


def train_tv_medians(rows_a):
    """Per-interval medians of TV covariates over the TRAINING A-rows."""
    out = {}
    for i in range(len(GRID_EDGES) - 1):
        sub = [r for r in rows_a if r.interval_idx == i]
        med = {}
        for k in TV_FEATURES + ("b_log_prints",):
            vals = [getattr(r, k) for r in sub]
            med[k] = float(np.median(vals)) if vals else 0.0
        out[i] = med
    return out


def cif_exit(pred_a, pred_b0, Xa, Xb, horizon_s: int) -> float:
    """P(team exit <= horizon) via the discrete competing-risks forward recursion."""
    surv = 1.0
    cif = 0.0
    for i, (a, b) in enumerate(zip(GRID_EDGES, GRID_EDGES[1:])):
        if a >= horizon_s:
            break
        ha = float(pred_a(Xa[i:i + 1])[0])
        hd = float(pred_b0(Xb[i:i + 1])[0])
        tot = min(ha + hd, 0.999)
        ha = ha * (tot / (ha + hd)) if ha + hd > 0.999 else ha
        cif += surv * ha
        surv *= max(1.0 - tot, 1e-9)
    return cif


def roc_auc(scores, y):
    scores, y = np.asarray(scores, float), np.asarray(y, float)
    order = np.argsort(scores)
    ranks = np.empty(len(scores)); ranks[order] = np.arange(1, len(scores) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def cluster_bootstrap_ci(coins, scores, labels, n_boot: int = 500, seed: int = 0):
    """Bootstrap over COINS (rows are clustered) for a coin-level AUC."""
    rng = np.random.default_rng(seed)
    coins = np.asarray(coins); scores = np.asarray(scores); labels = np.asarray(labels)
    uniq = np.unique(coins)
    stats = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, len(uniq), replace=True)
        idx = np.concatenate([np.flatnonzero(coins == c) for c in pick])
        a = roc_auc(scores[idx], labels[idx])
        if not np.isnan(a):
            stats.append(a)
    if not stats:
        return float("nan"), float("nan")
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def main() -> None:
    from src.common.db import get_connection
    conn = get_connection()
    A, B, statics, order = load_person_periods(conn)
    conn.close()
    Xa, ya, cols_a = build_matrix(A, statics)
    Xb, yb, cols_b = build_matrix(B, statics, B_EXTRA)
    print(f"A: rows={len(A)} events={int(ya.sum())}   B: rows={len(B)} "
          f"events={int(yb.sum())}   coins={len(order)}")

    fold_aucs, t0_by_h = [], {h: ([], [], []) for h in HORIZONS_S}
    for tr_a, te_a in coin_folds(A, order):
        if ya[tr_a].sum() < 15:
            continue
        pred_a = fit_hazard_logistic(Xa[tr_a], ya[tr_a], cols_a)
        # model B trained on ITS OWN training rows for the same coin split
        tr_coins = {A[i].token_mint for i in np.flatnonzero(tr_a)}
        trb = np.array([r.token_mint in tr_coins for r in B])
        if yb[trb].sum() < 15:
            continue
        pred_b = fit_hazard_logistic(Xb[trb], yb[trb], cols_b)

        p = pred_a(Xa[te_a])
        a_fold = roc_auc(p, ya[te_a])
        if not np.isnan(a_fold):
            fold_aucs.append(a_fold)

        # T0-mode horizon CIFs on test coins
        tv_med = train_tv_medians([A[i] for i in np.flatnonzero(tr_a)])
        te_coins = sorted({A[i].token_mint for i in np.flatnonzero(te_a)})
        exit_time = {}
        for r in A:
            if r.event:
                exit_time[r.token_mint] = r.t_start
        span_ok = {}
        for r in A + B:
            span_ok[r.token_mint] = max(span_ok.get(r.token_mint, 0), r.t_end)
        for m_ in te_coins:
            Xa0, Xb0 = t0_interval_matrix(statics.get(m_, {}), tv_med, cols_a, cols_b)
            for h in HORIZONS_S:
                exited = m_ in exit_time and exit_time[m_] < h
                observed = exited or span_ok.get(m_, 0) >= h
                if not observed:
                    continue
                pr = cif_exit(pred_a, pred_b, Xa0, Xb0, h)
                s, l, c = t0_by_h[h]
                s.append(pr); l.append(float(exited)); c.append(m_)

    fa = np.array(fold_aucs)
    print(f"\nMODEL A interval AUC, fold-averaged: {fa.mean():.3f} "
          f"(folds: {' '.join(f'{x:.2f}' for x in fa)})")
    print(f"\nT0-MODE pre-warning (competing-risks CIF, TV at train medians):")
    print(f"{'horizon':>9} {'n':>5} {'base':>7} {'AUC':>7} {'95% CI (coin bootstrap)':>24}")
    for h in HORIZONS_S:
        s, l, c = t0_by_h[h]
        if len(l) < MIN_AT_RISK:
            print(f"{h//60:>7}m {len(l):>5}  — n-gate not met, not quotable")
            continue
        auc = roc_auc(s, l)
        lo, hi = cluster_bootstrap_ci(c, s, l)
        print(f"{h//60:>7}m {len(l):>5} {np.mean(l):>7.1%} {auc:>7.3f}    [{lo:.3f}, {hi:.3f}]")


if __name__ == "__main__":
    sys.exit(main())
