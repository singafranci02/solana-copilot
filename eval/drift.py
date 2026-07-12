"""Phase 6 — drift monitoring. A model that isn't watched silently stops working.

Three things decay independently, so we track all three:

  1. LABEL drift  — the base rate itself moves (ours: distribute 56.8%→68.6%,
     rug@4h ~87%→90.8% inside one observation window). This alone inflates how
     accurate a SKIP-everything rule *looks* while the real edge erodes.
  2. FEATURE drift — the input distribution shifts (teams change tactics). Measured
     per feature as PSI (population stability index) recent-vs-reference.
  3. MODEL decay  — out-of-time ROC-AUC per rolling window. A previously strong
     feature whose importance collapses is a sign the evasion was learned.

Run:
    uv run python -m eval.drift [--window 2] [--target rug] [--horizon 4]

PSI reading: <0.10 stable · 0.10–0.25 moderate shift · >0.25 significant shift.
"""

from __future__ import annotations

import sys
import time

import numpy as np

from eval._common import load_samples, replay, distribute_score
from eval.model import (
    feature_names, build_matrix_nan, TRAINERS, platt_fit, platt_apply,
    roc_auc, _label,
)

PSI_MODERATE, PSI_SIGNIFICANT = 0.10, 0.25


def psi(ref: np.ndarray, cur: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index between a reference and current sample."""
    ref, cur = ref[~np.isnan(ref)], cur[~np.isnan(cur)]
    if len(ref) < 20 or len(cur) < 20:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    r, _ = np.histogram(ref, bins=edges)
    c, _ = np.histogram(cur, bins=edges)
    r = np.clip(r / max(r.sum(), 1), 1e-4, None)
    c = np.clip(c / max(c.sum(), 1), 1e-4, None)
    return float(np.sum((c - r) * np.log(c / r)))


def _day_windows(samples, days: int):
    """Split samples into consecutive `days`-wide buckets by graduated_at."""
    if not samples:
        return []
    t0, t1 = samples[0].graduated_at, samples[-1].graduated_at
    span = days * 86400
    out = []
    lo = t0
    while lo <= t1:
        w = [s for s in samples if lo <= s.graduated_at < lo + span]
        if w:
            out.append((lo, w))
        lo += span
    return out


def main() -> None:
    args = sys.argv[1:]
    win_days = int(args[args.index("--window") + 1]) if "--window" in args else 2
    target = args[args.index("--target") + 1] if "--target" in args else "rug"
    horizon = int(args[args.index("--horizon") + 1]) if "--horizon" in args else 4

    samples = [s for s in load_samples() if _label(s, horizon, target) is not None]
    samples.sort(key=lambda s: s.graduated_at)
    wins = _day_windows(samples, win_days)
    if len(wins) < 2:
        print("need at least 2 windows of data")
        return

    fmt = "%m-%d"
    print(f"DRIFT MONITOR · target={target} +{horizon}h · {win_days}-day windows · "
          f"{len(samples)} labeled")

    # ── 1. label drift ────────────────────────────────────────────────────────
    print(f"\n══ 1. LABEL drift (base rate) ══")
    print(f"  {'window':<12}{'n':>6}{'base':>9}{'rule SKIP%':>12}{'rule ROC':>10}")
    bases = []
    for lo, w in wins:
        y = np.array([_label(s, horizon, target) for s in w])
        rule = np.array([distribute_score(*replay(s.features)) for s in w])
        skip = np.mean([replay(s.features)[0] == "SKIP" for s in w])
        r = roc_auc(rule, y)
        bases.append(y.mean())
        print(f"  {time.strftime(fmt, time.gmtime(lo)):<12}{len(w):>6}{y.mean():>8.1%}"
              f"{skip:>11.1%}{r:>10.3f}")
    delta = bases[-1] - bases[0]
    flag = "⚠ MOVING" if abs(delta) > 0.05 else "stable"
    print(f"  → base rate moved {delta:+.1%} across the span  [{flag}]")
    print("    (a rising base rate inflates apparent SKIP accuracy while the real edge erodes)")

    # ── 2. feature drift (PSI, first window = reference) ──────────────────────
    keys = feature_names(samples, set())
    ref_s = wins[0][1]
    cur_s = wins[-1][1]
    Xref, Xcur = build_matrix_nan(ref_s, keys), build_matrix_nan(cur_s, keys)
    print(f"\n══ 2. FEATURE drift · PSI {time.strftime(fmt, time.gmtime(wins[0][0]))}"
          f" → {time.strftime(fmt, time.gmtime(wins[-1][0]))} ══")
    scored = sorted(
        ((psi(Xref[:, i], Xcur[:, i]), keys[i]) for i in range(len(keys))),
        key=lambda t: -(t[0] if t[0] == t[0] else -1),
    )
    shown = 0
    for v, name in scored:
        if v != v:
            continue
        tag = ("⚠ SIGNIFICANT" if v > PSI_SIGNIFICANT else
               "moderate" if v > PSI_MODERATE else "stable")
        if v > PSI_MODERATE or shown < 8:
            print(f"  {name:<32}PSI={v:>6.3f}  {tag}")
            shown += 1
    n_sig = sum(1 for v, _ in scored if v == v and v > PSI_SIGNIFICANT)
    print(f"  → {n_sig} feature(s) with significant shift (PSI > {PSI_SIGNIFICANT})")

    # ── 3. model decay: rolling out-of-time ROC ───────────────────────────────
    print(f"\n══ 3. MODEL decay (GBM trained on all prior windows, tested on next) ══")
    print(f"  {'eval window':<14}{'n':>6}{'base':>9}{'model ROC':>11}{'rule ROC':>10}{'edge':>8}")
    X = build_matrix_nan(samples, keys)
    y = np.array([_label(s, horizon, target) for s in samples])
    idx0 = 0
    bounds = []
    for lo, w in wins:
        bounds.append((idx0, idx0 + len(w)))
        idx0 += len(w)

    rocs = []
    for k in range(2, len(wins)):          # need ≥2 windows of history to train
        tr_end = bounds[k][0]
        te_a, te_b = bounds[k]
        if tr_end < 150 or te_b - te_a < 25:
            continue
        try:
            pred = TRAINERS["gbm"](X[:tr_end], y[:tr_end])
            # Platt on an inner time-split of train
            icut = int(tr_end * 0.8)
            inner = TRAINERS["gbm"](X[:icut], y[:icut])
            cal = platt_fit(inner(X[icut:tr_end]), y[icut:tr_end])
            p = platt_apply(cal, pred(X[te_a:te_b]))
        except Exception as exc:
            print(f"  (fold skipped: {exc})")
            continue
        yy = y[te_a:te_b]
        rule = np.array([distribute_score(*replay(s.features)) for s in samples[te_a:te_b]])
        mr, rr = roc_auc(p, yy), roc_auc(rule, yy)
        rocs.append(mr)
        print(f"  {time.strftime(fmt, time.gmtime(wins[k][0])):<14}{len(yy):>6}"
              f"{yy.mean():>8.1%}{mr:>11.3f}{rr:>10.3f}{mr - rr:>+8.3f}")

    if len(rocs) >= 2:
        trend = rocs[-1] - rocs[0]
        state = ("⚠ DECAYING — re-fit / investigate evasion" if trend < -0.05
                 else "holding" if abs(trend) <= 0.05 else "improving")
        print(f"  → model ROC trend {trend:+.3f} across windows  [{state}]")

    # ── 4. promotion gate ─────────────────────────────────────────────────────
    print(f"\n══ 4. PROMOTION GATE (no silent model swaps) ══")
    if not rocs:
        print("  INSUFFICIENT DATA — hold.")
    else:
        te_a, te_b = bounds[len(wins) - 1]
        yy = y[te_a:te_b]
        rule_last = roc_auc(
            np.array([distribute_score(*replay(s.features)) for s in samples[te_a:te_b]]), yy)
        model_last = rocs[-1]
        margin = model_last - rule_last
        ok = margin > 0.05 and model_last > 0.65
        print(f"  most recent window: model ROC {model_last:.3f} vs rules {rule_last:.3f} "
              f"(margin {margin:+.3f})")
        print(f"  gate: model must beat rules by >0.05 AND exceed 0.65 → "
              f"{'✅ PASS (eligible for live second-opinion)' if ok else '❌ HOLD'}")
    print("  NOTE: hard-SKIP rules stay in front of any model — they are near-deterministic")
    print("  on-chain facts, not probabilities, and must not be softened.")

    print("\n⚠ CONFOUND: some feature drift above is SELF-INDUCED, not adversarial —")
    print("  we changed the pipeline mid-window (wallet_graph pair cap → graph_hits;")
    print("  new market/social captures coming online). Treat PSI on those as OUR change,")
    print("  not teams evolving. Only features we did not touch are clean drift signals.")


if __name__ == "__main__":
    main()
