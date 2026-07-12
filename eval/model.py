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
                 target: str = "distribute"):
    """Expanding-window folds. Returns pooled OUT-OF-TIME predictions."""
    labeled = [s for s in samples if _label(s, horizon, target) is not None]
    labeled.sort(key=lambda s: s.graduated_at)
    keys = feature_names(labeled, drop)
    X = build_matrix(labeled, keys)
    y = np.array([_label(s, horizon, target) for s in labeled])

    n = len(labeled)
    start = int(n * 0.4)                      # first fold trains on the first 40%
    edges = np.linspace(start, n, n_folds + 1).astype(int)

    oot_p, oot_pc, oot_y, oot_idx = [], [], [], []
    for f in range(n_folds):
        tr_end, te_end = edges[f], edges[f + 1]
        if te_end - tr_end < 10 or tr_end < 50:
            continue
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[tr_end:te_end], y[tr_end:te_end]

        mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        Ztr = np.clip((Xtr - mu) / sd, -5, 5)
        Zte = np.clip((Xte - mu) / sd, -5, 5)

        # inner split of TRAIN (by time) to fit the calibrator — never on test
        icut = int(len(Ztr) * 0.8)
        w, b = fit_logistic(Ztr[:icut], ytr[:icut])
        cal = isotonic_fit(predict(Ztr[icut:], w, b), ytr[icut:])
        # refit on full train for the final predictor
        w, b = fit_logistic(Ztr, ytr)

        p = predict(Zte, w, b)
        oot_p.append(p)
        oot_pc.append(isotonic_apply(cal, p))
        oot_y.append(yte)
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

    samples = load_samples()
    res = walk_forward(samples, horizon, folds, drop, target)
    if res is None:
        print("not enough data for walk-forward yet")
        return
    p_raw, p_cal, y, te, keys, X, yall = res

    print(f"EXPANDING WALK-FORWARD · target={target} · +{horizon}h · {folds} folds · "
          f"out-of-time n={len(y)}  (features={X.shape[1]}, dropped={sorted(drop) or 'none'})")
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

    # feature importance from a full-sample refit (direction + magnitude)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    w, _ = fit_logistic(np.clip((X - mu) / sd, -5, 5), yall)
    names = keys + [k + "__missing" for k in keys]
    print("\n══ top features (+ predicts DISTRIBUTE, − predicts clean) ══")
    for i in np.argsort(-np.abs(w))[:14]:
        print(f"  {names[i]:<34}{w[i]:+.3f}")

    print("\nNOTE: research only — not wired into the live verdict. Any public claim must")
    print("quote rug precision at a stated operating point, not just distribution ROC.")


if __name__ == "__main__":
    main()
