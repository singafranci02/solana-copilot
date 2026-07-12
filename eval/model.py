"""Phase 3 — fitted, calibrated verdict model, validated under the Phase-0 harness.

Research deliverable, NOT live. Per docs/RESEARCH_PLAN.md the fitted model runs as
a second opinion beside verdict_rules_v2 until it is validated; nothing here
touches the live verdict path.

Discipline enforced here:
  - EXPANDING WALK-FORWARD by graduated_at. Never a random split, never a single
    holdout — memecoin structure drifts and is adversarial.
  - Features come only from graduation_feature_snapshot (frozen at verdict time),
    so they are point-in-time by construction. The rule's own output (verdict /
    confidence) is EXCLUDED so we measure independent discrimination.
  - Probabilities are CALIBRATED (isotonic / PAV, fit on train only).
  - Honest reporting: the primary target is will_distribute, but precision/recall
    is ALSO quoted on the rug outcome at stated operating points — because
    "ROC 0.84 on distribution" and "picks still rug ~78%" are both true and only
    quoting the first would be dishonest.

    uv run python -m eval.model [--target distribute|rug] [--horizon 4] [--folds 5] [--drop F]
"""

from __future__ import annotations

import sys

import numpy as np

from eval._common import (
    load_samples, replay, distribute_score, average_precision, brier,
    calibration_bins, day_bucket,
)

RULE_OUTPUT_KEYS = {"verdict", "confidence"}   # never features — that's circular


# ── features ──────────────────────────────────────────────────────────────────

def feature_names(samples, drop: set[str]) -> list[str]:
    keys = sorted({
        k for s in samples for k, v in s.features.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and k not in RULE_OUTPUT_KEYS and k not in drop
    })
    return keys


def build_matrix(samples, keys: list[str]) -> np.ndarray:
    """Numeric features + explicit missing-indicators (missingness is signal)."""
    X = np.array([[float(s.features.get(k) or 0.0) for k in keys] for s in samples])
    M = np.array([[1.0 if s.features.get(k) is None else 0.0 for k in keys] for s in samples])
    return np.hstack([X, M])


def build_matrix_nan(samples, keys: list[str]) -> np.ndarray:
    """Raw features with NaN for missing — GBM (HistGradientBoosting) splits on NaN
    natively, which is stronger than 0-fill + indicators."""
    return np.array([[float(s.features[k]) if s.features.get(k) is not None else np.nan
                      for k in keys] for s in samples])


# ── model (pure numpy — no sklearn in this env) ───────────────────────────────

def fit_logistic(Z: np.ndarray, y: np.ndarray, lam: float = 1e-2,
                 iters: int = 4000, lr: float = 0.1) -> tuple[np.ndarray, float]:
    w = np.zeros(Z.shape[1]); b = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(Z @ w + b)))
        w -= lr * (Z.T @ (p - y) / len(y) + lam * w)
        b -= lr * float(np.mean(p - y))
    return w, b


def predict(Z: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(Z @ w + b)))


# ── pluggable trainers: return a fitted predictor callable p(X)->prob ──────────

def train_logistic(Xtr, ytr):
    """Median-impute (train stats) + missing-indicators + standardize + L2 logistic.

    Imputation stats come from TRAIN only. Missingness is kept as its own feature —
    it is signal here (a field is absent because a stage didn't fire).
    """
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)          # all-NaN column → 0

    def prep(X):
        M = np.isnan(X).astype(float)
        return np.hstack([np.where(np.isnan(X), med, X), M])

    A = prep(Xtr)
    mu, sd = A.mean(0), A.std(0) + 1e-9
    w, b = fit_logistic(np.clip((A - mu) / sd, -5, 5), ytr)
    return lambda X: predict(np.clip((prep(X) - mu) / sd, -5, 5), w, b)


def _impute_prep(Xtr):
    """Median-impute (train stats) + missing-indicators. Shared by the tree models."""
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)

    def prep(X):
        M = np.isnan(X).astype(float)
        return np.hstack([np.where(np.isnan(X), med, X), M])
    return prep


def train_gbm(Xtr, ytr):
    """Gradient-boosted trees — captures the non-linear concentration × speed ×
    reputation interactions a linear score cannot.

    Uses the classic GradientBoostingClassifier rather than HistGradientBoosting:
    the histogram binner trips a numpy-2.4 stride bug on this build. We impute
    explicitly instead of relying on native NaN handling.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    prep = _impute_prep(Xtr)
    m = GradientBoostingClassifier(
        n_estimators=300, learning_rate=0.05, max_depth=3,
        subsample=0.8, min_samples_leaf=30, random_state=0)
    m.fit(prep(Xtr), ytr)
    return lambda X: m.predict_proba(prep(X))[:, 1]


TRAINERS = {"logistic": train_logistic, "gbm": train_gbm}


def platt_fit(p: np.ndarray, y: np.ndarray):
    """1-D Platt scaling: logistic on logit(score). Robust for extreme base rates."""
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    w, b = fit_logistic(z.reshape(-1, 1), y, lam=0.0, iters=2000, lr=0.5)
    return (w, b)


def platt_apply(cal, p: np.ndarray) -> np.ndarray:
    w, b = cal
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    return predict(z.reshape(-1, 1), w, b)


def isotonic_fit(p: np.ndarray, y: np.ndarray):
    """Pool-adjacent-violators isotonic regression → a calibration map."""
    order = np.argsort(p)
    xs, ys = p[order].astype(float), y[order].astype(float)
    v = ys.copy(); wgt = np.ones(len(ys))
    i = 0
    while i < len(v) - 1:
        if v[i] <= v[i + 1] + 1e-12:
            i += 1
            continue
        # pool the violating block backwards
        new_w = wgt[i] + wgt[i + 1]
        new_v = (v[i] * wgt[i] + v[i + 1] * wgt[i + 1]) / new_w
        v[i] = new_v; wgt[i] = new_w
        v = np.delete(v, i + 1); wgt = np.delete(wgt, i + 1); xs = np.delete(xs, i + 1)
        if i > 0:
            i -= 1
    return xs, v


def isotonic_apply(cal, p: np.ndarray) -> np.ndarray:
    xs, v = cal
    if len(xs) == 0:
        return p
    return np.interp(p, xs, v, left=v[0], right=v[-1])


def roc_auc(scores: np.ndarray, y: np.ndarray) -> float:
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(scores)
    ranks = np.empty(len(scores)); ranks[order] = np.arange(1, len(scores) + 1)
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


# ── walk-forward ──────────────────────────────────────────────────────────────

def _label(s, horizon: int, target: str):
    """Two heads (Phase 2): the structural target and the money target.

    They are NOT the same problem — a model fit on `distribute` discriminates team
    dumping well but does NOT minimise rugs. Fit on the target you actually want.
    """
    if target == "distribute":
        v = s.distribute.get(horizon)
        return None if v is None else (1.0 if v else 0.0)
    if target == "rug":
        o = s.outcome.get(horizon)
        return None if o not in ("moon", "ok", "rug") else (1.0 if o == "rug" else 0.0)
    raise ValueError(target)


def walk_forward(samples, horizon: int, n_folds: int, drop: set[str],
                 target: str = "distribute", model: str = "logistic",
                 calib: str = "isotonic"):
    """Expanding-window folds. Returns pooled OUT-OF-TIME predictions.

    model ∈ {logistic, gbm}; calib ∈ {none, isotonic, platt}. The calibrator is
    fit on a time-inner-split of TRAIN only — never on the evaluation slice.
    """
    labeled = [s for s in samples if _label(s, horizon, target) is not None]
    labeled.sort(key=lambda s: s.graduated_at)
    keys = feature_names(labeled, drop)
    X = build_matrix_nan(labeled, keys)       # NaN-aware; logistic nan_to_num's it
    y = np.array([_label(s, horizon, target) for s in labeled])
    trainer = TRAINERS[model]

    n = len(labeled)
    edges = np.linspace(int(n * 0.4), n, n_folds + 1).astype(int)

    oot_p, oot_pc, oot_y, oot_idx = [], [], [], []
    for f in range(n_folds):
        tr_end, te_end = edges[f], edges[f + 1]
        if te_end - tr_end < 10 or tr_end < 50:
            continue
        Xtr, ytr = X[:tr_end], y[:tr_end]

        # calibrator on an inner time-split of train
        cal = None
        if calib != "none":
            icut = int(len(Xtr) * 0.8)
            if icut > 40 and len(ytr[icut:]) > 20:
                inner = trainer(Xtr[:icut], ytr[:icut])
                pin = inner(Xtr[icut:])
                cal = (calib, isotonic_fit(pin, ytr[icut:]) if calib == "isotonic"
                       else platt_fit(pin, ytr[icut:]))

        pred = trainer(Xtr, ytr)              # final predictor on full train
        p = pred(X[tr_end:te_end])
        if cal is None:
            pc = p
        elif cal[0] == "isotonic":
            pc = isotonic_apply(cal[1], p)
        else:
            pc = platt_apply(cal[1], p)
        oot_p.append(p); oot_pc.append(pc); oot_y.append(y[tr_end:te_end])
        oot_idx.extend(range(tr_end, te_end))

    if not oot_p:
        return None
    return (np.concatenate(oot_p), np.concatenate(oot_pc), np.concatenate(oot_y),
            [labeled[i] for i in oot_idx], keys, X, y)


# ── report ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    horizon = int(args[args.index("--horizon") + 1]) if "--horizon" in args else 4
    folds = int(args[args.index("--folds") + 1]) if "--folds" in args else 5
    drop = {args[args.index("--drop") + 1]} if "--drop" in args else set()
    target = args[args.index("--target") + 1] if "--target" in args else "distribute"
    model = args[args.index("--model") + 1] if "--model" in args else "logistic"
    calib = args[args.index("--calib") + 1] if "--calib" in args else "isotonic"

    samples = load_samples()
    if "--with-topology" in args:
        from src.common.db import get_connection
        from eval.topology import load_topology
        conn = get_connection()
        try:
            topo = load_topology(conn)
        finally:
            conn.close()
        hit = 0
        for s in samples:
            t = topo.get(s.token_mint)
            if t:
                s.features = {**s.features, **t}
                hit += 1
        print(f"[topology merged into {hit}/{len(samples)} samples]")

    res = walk_forward(samples, horizon, folds, drop, target, model, calib)
    if res is None:
        print("not enough data for walk-forward yet")
        return
    p_raw, p_cal, y, te, keys, X, yall = res

    print(f"EXPANDING WALK-FORWARD · model={model} · calib={calib} · target={target} · "
          f"+{horizon}h · {folds} folds · out-of-time n={len(y)}  "
          f"(features={X.shape[1]}, dropped={sorted(drop) or 'none'})")
    print(f"span {day_bucket(te[0].graduated_at)} → {day_bucket(te[-1].graduated_at)}")

    base = y.mean()
    rule = np.array([distribute_score(*replay(s.features)) for s in te])
    print(f"\n══ target={target} (base={base:.1%}) ══")
    print(f"  {'':<22}{'ROC-AUC':>9}{'PR-AUC':>9}{'Brier':>9}")
    print(f"  {'rules (verdict_v2)':<22}{roc_auc(rule, y):>9.3f}{average_precision(rule, y):>9.3f}{brier(rule, y):>9.3f}")
    print(f"  {'model (raw)':<22}{roc_auc(p_raw, y):>9.3f}{average_precision(p_raw, y):>9.3f}{brier(p_raw, y):>9.3f}")
    print(f"  {'model (calibrated)':<22}{roc_auc(p_cal, y):>9.3f}{average_precision(p_cal, y):>9.3f}{brier(p_cal, y):>9.3f}")

    print("\n  calibration of the model (reliability):")
    for mid, pred, act, c in calibration_bins(p_cal, y, 8):
        bar = "█" * int(round(act * 20))
        print(f"    p≈{pred:4.2f}  actual={act:5.1%}  n={c:<4} {bar}")

    # ── the honest part: what happens to the MONEY outcome at real operating points
    for H in (4, 24):
        rug = np.array([1.0 if s.outcome.get(H) == "rug" else 0.0 for s in te])
        have = np.array([s.outcome.get(H) is not None for s in te])
        if have.sum() < 30:
            continue
        rb = rug[have].mean()
        print(f"\n══ MONEY OUTCOME · rug @{H}h (base={rb:.1%}, n={int(have.sum())}) ══")
        print(f"  {'selector':<22}{'n':>5}{'rug%':>8}{'vs base':>9}{'clean-precision':>17}")
        for frac in (0.05, 0.10, 0.20):
            k = max(int(have.sum() * frac), 1)
            idx = np.argsort(p_cal[have])[:k]           # model's cleanest
            r = rug[have][idx].mean()
            print(f"  {'model cleanest ' + str(int(frac*100)) + '%':<22}{k:>5}{r:>7.1%}{r - rb:>+8.1%}{1 - r:>16.1%}")
        sound = np.array([replay(s.features)[0] == "STRUCTURALLY_SOUND" for s in te])
        sm = sound & have
        if sm.sum():
            r = rug[sm].mean()
            print(f"  {'rule SOUND':<22}{int(sm.sum()):>5}{r:>7.1%}{r - rb:>+8.1%}{1 - r:>16.1%}")
        print(f"  {'ALL (buy-all)':<22}{int(have.sum()):>5}{rb:>7.1%}{0:>+8.1%}{1 - rb:>16.1%}")

    # feature importance from a full-sample logistic refit (direction + magnitude)
    Xf = build_matrix(te, keys)   # 0-fill+indicator form for interpretable coefs
    yf = y
    mu, sd = Xf.mean(0), Xf.std(0) + 1e-9
    w, _ = fit_logistic(np.clip((Xf - mu) / sd, -5, 5), yf)
    names = keys + [k + "__missing" for k in keys]
    sign = "DISTRIBUTE" if target == "distribute" else "RUG"
    print(f"\n══ top features (+ predicts {sign}, − predicts clean) ══")
    for i in np.argsort(-np.abs(w))[:14]:
        print(f"  {names[i]:<34}{w[i]:+.3f}")

    print("\nNOTE: research only — not wired into the live verdict. Any public claim must")
    print("quote rug precision at a stated operating point, not just distribution ROC.")


if __name__ == "__main__":
    main()
