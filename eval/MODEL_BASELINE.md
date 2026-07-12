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

---

# Phase 6 — Drift monitoring (`uv run python -m eval.drift`)

Recorded **2026-07-13**, 2-day windows, target=rug@4h, n=3256 labeled.

## 1. Label drift — CONFIRMED and material
| window | n | rug base | rule SKIP% | rule ROC |
|---|---|---|---|---|
| 07-03 | 820 | 87.8% | 80.5% | 0.515 |
| 07-05 | 855 | 87.4% | 86.5% | 0.443 |
| 07-07 | 760 | 89.2% | 87.9% | 0.575 |
| 07-09 | 325 | 91.4% | 89.5% | 0.607 |
| 07-11 | 497 | **94.2%** | **93.1%** | 0.644 |

**Base rate +6.3pt in 8 days.** Note the rule's SKIP% tracks the base rate almost
exactly (80.5% → 93.1%) — it is *following* the market, not predicting it. This is
precisely how a rising base rate flatters a SKIP-everything strategy while the real
edge erodes.

## 2. Feature drift — 11 features PSI > 0.25
Largest: `graph_hits` (4.48), `team_first_buy_offset_s` (1.45),
`funder_choreography_n` (1.11), `team_size` (0.47).

**⚠ CONFOUND — must not be misread as adversarial evasion.** Much of this is
**self-induced**: we changed the pipeline mid-window (wallet_graph pair cap ⇒
`graph_hits` collapsed by design; new market/social captures came online; team
scoring shipped). Only features we did *not* touch are clean drift signals. The
monitor prints this warning inline. Re-baseline PSI from a post-change reference
window before treating any of it as teams adapting.

## 3. Model decay — NOT decaying; it is *improving* and the edge is stable
| eval window | n | base | model ROC | rule ROC | edge |
|---|---|---|---|---|---|
| 07-07 | 760 | 89.2% | 0.838 | 0.575 | **+0.263** |
| 07-09 | 325 | 91.4% | 0.908 | 0.607 | **+0.300** |
| 07-11 | 497 | 94.2% | 0.933 | 0.644 | **+0.289** |

Trend **+0.094** (improving). The model's advantage over the rules is **stable at
~+0.29 ROC on every window** — it is not a one-window artifact.

## 4. Promotion gate
`model ROC > rules + 0.05 AND model ROC > 0.65` on the **most recent** window.
Current: **0.933 vs 0.644 (margin +0.289) → ✅ PASS** — eligible to run as a live
*second opinion*. Hard-SKIP rules stay **in front of** any model (near-deterministic
on-chain facts, not probabilities). No silent promotions.
