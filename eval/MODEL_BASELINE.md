# Phase 3 — Fitted model vs `verdict_rules_v2` (research result, NOT live)

Recorded **2026-07-12**. Regenerate:

```
uv run python -m eval.model --target distribute --horizon 4 --folds 5
uv run python -m eval.model --target rug        --horizon 4 --folds 5
uv run python -m eval.model --target distribute --folds 5 --drop unique_bc_buyers   # leakage guard
```

**Method.** Expanding walk-forward by `graduated_at` (5 folds, first trains on the
earliest 40%) — never a random split, never a single holdout. Features come only
from `graduation_feature_snapshot` (frozen at verdict time, point-in-time by
construction; replay fidelity 100%). The rule's own `verdict`/`confidence` are
**excluded** as features so we measure independent discrimination. L2 logistic
regression, pure numpy. Isotonic (PAV) calibration fit on a time-inner-split of
train only. Out-of-time span 2026-07-06 → 2026-07-12.

---

## 1. The headline: discrimination is real and large

### `will_distribute` head, +4h (OOT n=1910, base 65.9%)
| | ROC-AUC | PR-AUC | Brier |
|---|---|---|---|
| rules (`verdict_rules_v2`) | **0.580** (≈ coin-flip) | 0.642 | 0.233 |
| model (raw) | **0.858** | 0.894 | 0.134 |
| model (calibrated) | 0.855 | 0.897 | 0.138 |

Calibration is genuinely good — reliability is monotonic and near-diagonal
(p≈0.05 → 6.5% actual; p≈0.68 → 55.0%; p≈0.95 → 88.6%). `confidence` finally
means something.

**Leakage / robustness guard:** `unique_bc_buyers` dominates (coef −0.825), so we
refit without it — ROC-AUC stays **0.858**. The model is *not* a one-feature trick.

---

## 2. The honest part: distribution ≠ money

A model fit on `distribute` discriminates team-dumping well but **does not minimise
rugs**. You must fit the target you actually want (this is Phase 2, now proven
empirically rather than argued):

### `rug` head, +4h (OOT n=1906, **base 90.5%**)
| selector | n | rug % | **survival (clean-precision)** | lift vs buy-all |
|---|---|---|---|---|
| **rug-model cleanest 5%** | 95 | **68.4%** | **31.6%** | **3.3×** |
| rug-model cleanest 10% | 190 | 74.7% | 25.3% | 2.7× |
| *distribute*-model cleanest 5% | 94 | 77.7% | 22.3% | 2.3× |
| rule SOUND | 11 | 72.7% | 27.3% | 2.8× (n too small) |
| buy-all (baseline) | 1906 | 90.5% | 9.5% | 1.0× |

rug-head ROC-AUC **0.804** (rules: 0.563).

### `rug` head, +24h (OOT n=911, **base 94.5%**)
| selector | n | rug % | survival | lift |
|---|---|---|---|---|
| rug-model cleanest 5% | 45 | 82.2% | **17.8%** | **3.2×** |
| rug-model cleanest 10% | 91 | 84.6% | 15.4% | 2.8× |
| buy-all | 911 | 94.5% | 5.5% | 1.0× |

rug-head ROC-AUC **0.809**.

---

## 3. What may and may not be claimed

**Defensible (walk-forward, out-of-time, stated operating point):**
- Distribution-propensity ranking: **ROC-AUC 0.858** vs 0.580 for the rules, with
  calibrated probabilities (Brier 0.233 → 0.134).
- Rug-avoidance at a 5% selection rate: **31.6% survival vs a 9.5% base (3.3× lift)**
  at 4h; **17.8% vs 5.5% (3.2×)** at 24h.

**NOT claimable:**
- "We find coins that go up." Even the best-selected decile **still rugs 68–85%** of
  the time. The environment is brutal — 90.5% of graduations rug by 4h, 94.5% by 24h.
- Any headline quoting ROC alone. ROC 0.86 and "your picks still rug ~70%" are both
  true; quoting only the first would be dishonest.

**Caveats to close before productising:**
- Isotonic calibration *hurt* the rug head (ROC 0.804 → 0.752): the base rate is
  extreme and the calibration slice is small. Use raw scores for ranking; revisit
  calibration (Platt, or a larger calibration window) as data grows.
- Logistic only. GBM (per plan) is untested here and would likely add lift — and
  overfit risk. Test it under this same harness.
- ~6 days of out-of-time data. Drift is live (see below); re-fit cadence matters.

---

## 4. Drift (Phase 6 is not optional)
Distribute base rate **56.8% → 68.6%** and rug@4h base **~87% → 90.5%** over the
observation window. The environment is getting more extractive. Any fixed model
decays; promotion must be gated by this harness on a rolling schedule.
