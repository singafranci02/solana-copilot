# Phase 3 — Fitted model vs `verdict_rules_v2` (research result, NOT live)

Final. Recorded **2026-07-13**. Regenerate:

```
uv run python -m eval.model --target distribute --model gbm --calib platt
uv run python -m eval.model --target rug        --model gbm --calib platt
uv run python -m eval.model --target rug        --model gbm --calib platt --horizon 24
uv run python -m eval.model --target distribute --model gbm --drop unique_bc_buyers   # leakage guard
```

**Method.** Expanding walk-forward by `graduated_at` (5 folds; first trains on the
earliest 40%) — never random, never a single holdout. Features come only from
`graduation_feature_snapshot` (frozen at verdict time; point-in-time by
construction, replay fidelity 100%). The rule's own `verdict`/`confidence` are
**excluded** so we measure independent discrimination. Median-impute + explicit
missing-indicators (missingness is signal). Calibrator fit on a time-inner-split
of TRAIN only. Out-of-time span 2026-07-06 → 2026-07-12.

**Chosen config: GBM (gradient-boosted trees) + Platt scaling.**

---

## 1. Discrimination — the rules have none; the model has a lot

### `will_distribute`, +4h (OOT n≈1955, base 66.0%)
| model | ROC-AUC | PR-AUC | Brier |
|---|---|---|---|
| rules (`verdict_rules_v2`) | **0.580** (coin-flip) | 0.645 | 0.232 |
| logistic | 0.859 | 0.897 | 0.134 |
| **GBM** | **0.921** | **0.939** | **0.086** |

**Leakage guard:** dropping the dominant feature (`unique_bc_buyers`) leaves ROC
essentially unchanged — not a one-feature trick.

### `rug`, +4h (OOT n=1953, base 90.8%)
| model / calib | ROC-AUC | Brier |
|---|---|---|
| rules | 0.575 | 0.094 |
| logistic + none | 0.810 | 0.077 |
| **GBM + Platt** | **0.859** | **0.071** |
| GBM + isotonic | 0.849 | 0.070 |

**Calibration note (fixed):** isotonic *degraded* the rug head at the previous
checkpoint. **Platt scaling fixes it** — it is stable under the extreme (≈91%)
base rate where isotonic overfits the small calibration slice.

---

## 2. The money outcome — honest operating points

### rug @4h (base 90.8% → survival base **9.2%**)
| selector | n | rug % | **survival** | **lift** |
|---|---|---|---|---|
| **GBM cleanest 5%** | 97 | **61.9%** | **38.1%** | **4.1×** |
| GBM cleanest 10% | 195 | 65.6% | 34.4% | 3.7× |
| GBM cleanest 20% | 390 | 71.8% | 28.2% | 3.1× |
| rule SOUND | 11 | 72.7% | 27.3% | 3.0× (n too small) |
| buy-all | 1953 | 90.8% | 9.2% | 1.0× |

### rug @24h (base 94.5% → survival base **5.5%**)
| selector | n | rug % | survival | lift |
|---|---|---|---|---|
| GBM cleanest 10% | 91 | 83.5% | **16.5%** | **3.0×** |
| GBM cleanest 5% | 45 | 84.4% | 15.6% | 2.8× |
| buy-all | 911 | 94.5% | 5.5% | 1.0× |

---

## 3. What may and may not be claimed

**Defensible (walk-forward, out-of-time, stated operating point):**
- Distribution-propensity ranking: **ROC-AUC 0.921** vs **0.580** for the rules,
  calibrated (Brier 0.232 → 0.086).
- Rug-avoidance at a 5% selection rate: **38.1% survival vs a 9.2% base — 4.1× lift**
  at 4h; **16.5% vs 5.5% — 3.0×** at 24h (10% selection).

**NOT claimable:**
- "We find coins that go up." Even the best-selected 5% **still rugs 62%** at 4h and
  **84%** at 24h. The environment is brutal: 90.8% / 94.5% of graduations rug.
- Any headline quoting ROC alone. "ROC 0.92" and "your picks still rug 62%" are both
  true; quoting only the first is dishonest.

**Progress vs the previous checkpoint:** GBM lifted distribution ROC 0.858 → **0.921**
and rug survival-lift 3.3× → **4.1×**; Platt fixed the rug calibration.

---

## 4. Remaining before it can go live
- Not wired into the live verdict (runs as a research second opinion, per the plan).
- Retain the **hard-SKIP rules as a safety layer** in front of any model (they are
  near-deterministic facts, not probabilities).
- Re-fit cadence + promotion gate — see Phase 6. Drift is live: distribute base
  56.8% → 68.6%, rug@4h base ~87% → 90.8% within the observation window.
