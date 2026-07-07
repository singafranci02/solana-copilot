"""Walk-forward evaluator for any verdict function against outcomes.

Baseline = the live rule engine, replayed faithfully from frozen snapshots.
Primary target = will_distribute (structural; the product). moon (≥3×) is
reported as a secondary, low-ceiling target. Metrics: PR-AUC (primary — accuracy
is meaningless at this base rate), precision/recall/F1, Brier + calibration.

    uv run python -m eval.backtest [--horizon 4] [--bins 10]

Never shuffles; folds are expanding calendar-day windows on graduated_at.
"""

from __future__ import annotations

import sys

import numpy as np

from eval._common import (
    HORIZONS, Sample, load_samples, replay, distribute_score,
    average_precision, brier, prf1, calibration_bins, day_bucket,
)


def _arr(samples, keyfn):
    return np.array([keyfn(s) for s in samples], dtype=float)


def _report_horizon(samples: list[Sample], h: int, bins: int) -> None:
    # keep only samples whose horizon-h labels exist (post-verdict data present)
    dist = [s for s in samples if s.distribute.get(h) is not None]
    moon = [s for s in samples if s.outcome.get(h) is not None]

    print(f"\n══ horizon +{h}h ══════════════════════════════════════════")

    # ── will_distribute (primary) ─────────────────────────────────────────────
    if dist:
        scores = _arr(dist, lambda s: distribute_score(*replay(s.features)))
        y = _arr(dist, lambda s: 1.0 if s.distribute[h] else 0.0)
        pred_skip = _arr(dist, lambda s: 1.0 if replay(s.features)[0] == "SKIP" else 0.0)
        ap = average_precision(scores, y)
        base = y.mean()
        p, r, f = prf1(pred_skip, y)
        b = brier(scores, y)
        print(f"  will_distribute   n={len(dist):<4} base_rate={base:5.1%}  "
              f"PR-AUC={ap:.3f} (lift {ap/base if base else float('nan'):.2f}×)")
        print(f"    SKIP⇒distribute  precision={p:.3f} recall={r:.3f} F1={f:.3f}  "
              f"Brier={b:.3f}")
    else:
        print("  will_distribute   no labeled data yet")

    # ── moon (secondary) ──────────────────────────────────────────────────────
    if moon:
        mscore = _arr(moon, lambda s: 1.0 - distribute_score(*replay(s.features)))
        my = _arr(moon, lambda s: 1.0 if s.outcome[h] == "moon" else 0.0)
        pred_sound = _arr(moon, lambda s: 1.0 if replay(s.features)[0] == "STRUCTURALLY_SOUND" else 0.0)
        ap = average_precision(mscore, my)
        base = my.mean()
        p, r, f = prf1(pred_sound, my)
        print(f"  moon (≥3×)        n={len(moon):<4} base_rate={base:5.1%}  "
              f"PR-AUC={ap:.3f} (lift {ap/base if base else float('nan'):.2f}×)")
        if pred_sound.sum() > 0:
            print(f"    SOUND⇒moon       precision={p:.3f} recall={r:.3f} F1={f:.3f}")

    # ── calibration of the distribute probability ─────────────────────────────
    if dist:
        print("  calibration (distribute prob vs actual):")
        for mid, pred, act, c in calibration_bins(scores, y, bins):
            bar = "█" * int(round(act * 20))
            print(f"    p≈{pred:4.2f}  actual={act:5.1%}  n={c:<4} {bar}")


def _walk_forward(samples: list[Sample], h: int) -> None:
    """Expanding-window folds by calendar day (out-of-sample PR-AUC stability)."""
    buckets = sorted({day_bucket(s.graduated_at) for s in samples})
    if len(buckets) < 2:
        print("\n  walk-forward: <2 day-buckets — pooled result only "
              "(re-run as data accrues).")
        return
    print(f"\n══ walk-forward (expanding, by day) · +{h}h will_distribute ══")
    for i in range(1, len(buckets)):
        ev = [s for s in samples
              if day_bucket(s.graduated_at) == buckets[i]
              and s.distribute.get(h) is not None]
        if not ev:
            continue
        scores = _arr(ev, lambda s: distribute_score(*replay(s.features)))
        y = _arr(ev, lambda s: 1.0 if s.distribute[h] else 0.0)
        ap = average_precision(scores, y)
        base = y.mean()
        print(f"  train≤{buckets[i-1]}  eval {buckets[i]}: "
              f"n={len(ev):<4} base={base:5.1%} PR-AUC={ap:.3f}")


def main() -> None:
    args = sys.argv[1:]
    horizon = int(args[args.index("--horizon") + 1]) if "--horizon" in args else None
    bins = int(args[args.index("--bins") + 1]) if "--bins" in args else 10

    samples = load_samples()
    print(f"loaded {len(samples)} pipeline-v2 snapshots "
          f"({day_bucket(samples[0].graduated_at)} → {day_bucket(samples[-1].graduated_at)})")

    # fidelity self-check: replay must reproduce the stored verdict
    mism = sum(1 for s in samples if replay(s.features)[0] != s.stored_verdict
               and s.stored_verdict is not None)
    labeled = sum(1 for s in samples if s.stored_verdict is not None)
    print(f"replay fidelity: {labeled - mism}/{labeled} verdicts reproduced "
          f"({(labeled-mism)/labeled:.1%})" if labeled else "no stored verdicts")

    verdicts = [replay(s.features)[0] for s in samples]
    from collections import Counter
    print("verdict mix:", dict(Counter(verdicts)))

    horizons = [horizon] if horizon else list(HORIZONS)
    for h in horizons:
        _report_horizon(samples, h, bins)
    _walk_forward(samples, 4)

    print("\nNote: base rate here is among GRADUATIONS, not all launches. "
          "Structure buys rug-avoidance, not winner-picking — read PR-AUC + "
          "calibration, not accuracy.")


if __name__ == "__main__":
    main()
