# Phase 0 Baseline — `verdict_rules_v2`

Recorded **2026-07-06**. Every later phase is judged against these numbers
(`docs/RESEARCH_PLAN.md`, Phase 0 DoD). Regenerate with:

```
uv run python -m eval.backtest
uv run python -m eval.economic_backtest
uv run python -m eval.ablation
```

## Data
- **1,292** pipeline-v2 snapshots, span **2026-07-03 → 2026-07-06** (~3 days).
- **Replay fidelity: 1292/1292 (100%)** — the harness reconstructs the exact ctx
  from each frozen snapshot and runs the *live* `structural_read`, so the baseline
  is faithful and will auto-track future rule changes.
- Verdict mix: **SKIP 1068 · WATCH 181 · SOUND 43** (the ruleset is SKIP-heavy).
- ⚠ Base rates below are among **graduations**, not all launches. Structure buys
  **rug-avoidance, not winner-picking** — read PR-AUC + calibration, not accuracy.
- ⚠ 3 days of data. Everything here is **provisional**; we are in collection mode.
  Do not act on it — Phase 3 acts, once the set is larger and time-split is real.

## will_distribute (primary, structural target)

| horizon | n | base rate | PR-AUC | lift | SKIP⇒distribute P / R / F1 | Brier |
|---|---|---|---|---|---|---|
| +1h | 1215 | 55.2% | 0.574 | 1.04× | 0.633 / 0.943 / 0.758 | 0.283 |
| +4h | 1098 | 59.4% | 0.613 | 1.03× | 0.669 / 0.925 / 0.776 | 0.262 |
| +24h | 301 | 64.1% | 0.619 | 0.96× | 0.704 / 0.938 / 0.804 | 0.247 |

**Read:** the rule SKIPs ~83% of tokens and thereby catches ~93% of distributors
(high recall) at ~65% precision. But PR-AUC ≈ base rate (lift ~1.0×) — the rule
barely *ranks* distribute-likelihood; it can't discriminate *within* the SKIP
bucket. The confident-SKIP calibration bin (p≈0.91) does distribute 71–78% vs the
55–64% base, so the confident SKIPs are genuinely enriched — but the mid-confidence
SKIPs (p≈0.82) distribute only ~26%, i.e. **`confidence` is badly mis-calibrated**
(exactly the Phase-3 calibration target). Brier 0.25–0.28.

## moon (secondary; structure should NOT predict this)
- +1h / +4h: PR-AUC ≈ 0.025 (≈ base) — structure carries **no** winner signal, as expected.
- +24h: PR-AUC 0.342 on n=270 with only 3 moons — noise, ignore until the sample grows.

## Economic backtest — rug-avoidance edge (the headline)

| horizon | portfolio | n | rug | ok | moon | median mult |
|---|---|---|---|---|---|---|
| +1h | rule SOUND | 41 | **51.2%** | 46.3% | 2.4% | 0.27× |
| +1h | buy-all | 1218 | 80.7% | 16.6% | 2.7% | 0.02× |
| +4h | rule SOUND | 37 | **73.0%** | 24.3% | 2.7% | 0.12× |
| +4h | buy-all | 1099 | 87.4% | 10.5% | 2.2% | 0.02× |

**Rug-avoidance edge: −29.5pt at 1h, −14.4pt at 4h** (rule SOUND vs buy-all).
SOUND coins also retain far more value (median 0.12–0.27× vs 0.02×). The pipeline's
core claim holds: **SOUND meaningfully avoids rugs.** (24h SOUND n=2 — ignore; one
outlier multiple.)

## Ablation (+4h, remove one rule at a time)
Negative ΔPR-AUC ⇒ the rule carries distribute-ranking signal.

| rule | ΔPR-AUC | note |
|---|---|---|
| **smart_money** | **−0.048** | the only rule clearly carrying signal on current data |
| funder_reputation, creator_reputation, fingerprint, proven_wallets, exit_leader_ring | 0.000 | **inert — insufficient data** (n≥8 gates rarely met yet), not judgeable |
| wallet_graph | +0.034 | |
| team_supply | +0.096 | |
| bc_speed | +0.140 | |
| top_holder_concentration | +0.171 | removing it *improves* distribute-ranking |

**Read (provisional):** on 3 days, only **smart_money** helps rank distribution.
The concentration/speed hard-skips are SKIP-trigger-happy but not distribute-
predictive *for the 4h horizon* — removing them improves ranking, meaning they
compress everything into "will distribute" without discriminating. This is a
Phase-3 signal (re-weight or gate them), **not** a reason to change rules now.
The reputation/choreography rules are inert only because their n≥8 samples barely
exist yet — that's a data-maturity gap, not a verdict on the signal.

## What to beat
- **Distribution PR-AUC +4h: 0.613** (barely above the 0.594 base) — the number a
  fitted, calibrated model must lift, with a **calibrated** probability (Brier < 0.26).
- **Rug-avoidance edge +4h: −14.4pt** — a better model should widen this while
  keeping a usable SOUND count.


---

# Update — 2026-07-12 (10 days of data, 2.8× the original sample)

Re-run of the same harness. **Replay fidelity still 100%** (3617/3617).

## Data
- **3,617** pipeline-v2 snapshots, span **2026-07-03 → 2026-07-12**.
- Verdict mix: **SKIP 3138 (87%) · WATCH 423 · SOUND 56 (1.5%)**.

## will_distribute (primary)
| horizon | n | base | PR-AUC | lift | SKIP⇒dist P / R / F1 | Brier |
|---|---|---|---|---|---|---|
| +1h | 3485 | 58.2% | 0.574 | 0.99× | 0.651 / 0.968 / 0.779 | 0.278 |
| +4h | 3182 | 63.5% | 0.627 | 0.99× | 0.702 / 0.958 / 0.811 | 0.244 |
| +24h | 1655 | 68.9% | 0.660 | 0.96× | 0.749 / 0.959 / 0.841 | 0.214 |

**Ranking power is still ~zero (lift ≈ 1.0×)** — the rule SKIPs 87% of coins and
rides the base rate. Confidence remains uncalibrated. This is the Phase-3 gap.

## Economic — rug-avoidance (the thesis) — STRONGER with more data
| horizon | SOUND rug | buy-all rug | edge | SOUND median mult |
|---|---|---|---|---|
| +1h | **53.7%** (n=54) | 82.9% | **−29.2 pts** | 0.23× vs 0.02× |
| +4h | **72.9%** (n=48) | 89.2% | **−16.3 pts** | 0.13× vs 0.02× |
| +24h | **58.3%** (n=12) | 92.5% | **−34.2 pts** | 0.27× vs 0.02× |

Random control ≈ buy-all, so the edge is signal, not selection luck. SOUND moons
at 8.3% vs 0.9% baseline at 24h (n=12 — noisy, but directionally right).

## Read
The engine is an **excellent filter, a poor ranker**. The rare SOUND call (~1.5%,
≈5/day) roughly halves rug rate vs buying everything — a real, defensible edge that
is strengthening. But 87% of coins sit in an undifferentiated SKIP bucket with no
discrimination inside it, and moon-prediction is nil (PR-AUC 0.02) exactly as
predicted (structure ⇒ rug-avoidance, not winner-picking).

## Drift watch (Phase 6)
Distribute base rate climbed **56.8% → 68.6%** over the 10 days — the environment is
getting more extractive. Track this; it inflates apparent SKIP accuracy over time.

## What Phase 3 must beat
- **+4h distribution PR-AUC 0.627** (vs 0.635 base — i.e. beat *chance*, with a
  **calibrated** probability, Brier < 0.244).
- **+24h rug-avoidance edge −34.2 pts** — widen it while keeping a usable SOUND count.
- Now fitting on **3,182 labeled 4h rows** (vs 1,098 at the original baseline).
