# solana-copilot · Research & Engineering Plan (v2 → v3)

**Purpose of this document.** A phase-ordered plan to move the pipeline from hand-tuned
heuristics to a validated, fitted, sellable data product. Written to be executed top-to-bottom
by Claude Code. Each phase has a goal, concrete tasks, and a *Definition of Done* (DoD) that must
pass before moving on. Do not skip phases — later phases assume the harness and datasets from
earlier ones.

**North star.** The durable, defensible edge here is **distribution-propensity detection**
(is the team engineered to dump), not price prediction. Structure predicts *who dumps* well and
*what moons* poorly. Build and market accordingly. Everything below optimizes for a clean,
leak-free, reproducible dataset and verdict — because that is what is sellable and what survives
an adversarial, drifting environment.

**Hard rule that never changes.** Analysis only. No trade execution anywhere in this codebase.

**Current mode: COLLECT, don't sell (yet).** The near-term goal is to accrue the cleanest possible
dataset so the best algo can be built on top of it later. This reprioritizes the plan:

- **Immediate priority = collection fidelity.** Phase 0 (harness) and a collection-completeness audit
  come first. Phase 7 (productization) is **deferred** until the data and algo are proven — but the
  guardrails below (versioning, provenance, point-in-time correctness) still apply now, because
  retrofitting them later is painful and sometimes impossible.
- **Point-in-time external state is NON-RECOVERABLE — over-collect it now.** On-chain features can
  always be re-derived from the chain later. External point-in-time state cannot: social follower
  counts at graduation, DexScreener state, "who was a known rugger as of that date," the exact holder
  snapshot at verdict time. Once it passes, it's gone. **Capture everything at maximum fidelity now,
  even features you aren't using yet.** A captured-but-unused feature is cheap; a missing one is permanent.

---

## 0. Guardrails that apply to every phase

- **Point-in-time correctness is sacred.** The existing `graduation_feature_snapshot` design is the
  single most valuable thing in the current pipeline. Every new feature added anywhere must be
  computable *only* from data available at or before verdict time. Any feature that touches
  post-verdict data is a label, not a feature, and lives on the label side of the snapshot.
- **Never use random train/test splits.** Always split by time (walk-forward). Memecoin structure
  drifts and is adversarial; random splits leak the future and produce backtests that fail live.
- **Everything versioned.** Schema version, feature-set version, model version, and ruleset version
  are all recorded on every row. A buyer of this data needs to know exactly how any row was produced.
- **Reproducibility over cleverness.** Prefer a slightly worse model that reproduces exactly to a
  better one that can't be rebuilt from stored inputs.

---

## Phase 0 — Build the evaluation harness FIRST (before any model or rule change)

**Goal.** You cannot improve what you cannot measure, and right now the ruleset is untested priors.
Build the harness that scores *any* verdict function (current rules, future model) against outcomes.
This phase changes zero business logic — it only measures the logic you already have.

**Tasks**
1. `eval/backtest.py` — a walk-forward evaluator. Input: a verdict function + the historical
   `graduation_feature_snapshot` rows with their 1h/4h/24h outcomes. Split by `graduated_at` into
   expanding time windows (e.g. train ≤ week N, evaluate week N+1). No random shuffling anywhere.
2. Report, per horizon (1h/4h/24h) and per verdict class (SOUND/WATCH/SKIP):
   - **PR-AUC** on the rare positive class (this is the primary metric — accuracy is meaningless at a
     ~0.2% base rate; see Phase 1 base-rate notes).
   - Precision/recall/F1 for "team distributes" and for "moon (≥3×)".
   - **Calibration curve** + Brier score for `confidence`. The current
     `confidence = min(0.85, 0.5 + 0.1·|score|)` is a formula, not a probability — measure how far off it is.
3. `eval/economic_backtest.py` — the number that actually matters. Simulate three portfolios over
   the walk-forward window, **paper only, no execution**:
   - take every SOUND, avoid every SKIP;
   - buy everything that graduates (baseline);
   - random selection of graduations (control).
   Report the outcome distribution (moon/ok/rug rates, median MC multiple at each horizon) for each.
   The SOUND portfolio must beat both baselines on *rug-avoidance* to justify the pipeline.
4. `eval/ablation.py` — turn each factor and each hard-SKIP rule on/off and measure the delta on the
   metrics above. This tells you which of the hand-tuned rules actually carry signal.

**Definition of Done.** Running `eval/backtest.py` on the current rule-based verdict produces a full
report with PR-AUC, calibration, and the three-portfolio economic comparison. You now have a baseline
number to beat. **Record it** — every later phase is judged against this baseline.

---

## Phase 1 — Stand on public labeled datasets (benchmark + bootstrap)

**Goal.** Stop collecting the entire training signal from scratch. Three public datasets are directly
on-point; use them to (a) benchmark your detector against published work, (b) bootstrap labels, and
(c) sanity-check your coordination-supply estimates.

**Datasets to ingest** (add loaders under `data/external/`):

| dataset | what it gives you | use |
|---|---|---|
| **MELT / MemeTrans** (arXiv 2602.13480) | 41k+ Solana memecoin launches, 200M+ txns parsed into typed behavioral records (swap/wash/transfer/mint), **bundle-trace data linking accounts controlled by one entity**. Reports mean 36.5% of supply held by coordinated accounts. | Benchmark your `team_clusters.supply_pct`; fit/validate `Ecoord` weights against their bundle labels. |
| **SolRPDS** (arXiv 2504.07132) | First public Solana rug-pull dataset, ~4 yrs (2021–2024), suspected + confirmed rugs. | Prior for `creator_reputation` / `funder_reputation`; negative-class labels. |
| **Kamat GRW / RED-PUMP-2026-v1** (SSRN 6915560) | Survival analysis of 832,941 launches (May–Jun 2026); **860,213-launch record-level dataset released CC-BY-4.0 on Zenodo**, concept DOI 10.5281/zenodo.20633486. | Calibrate graduation base rates; social-presence features (Phase 5); hazard modeling (Phase 2). |

**Tasks**
1. Write loaders that normalize each dataset into your snapshot schema where fields overlap.
2. Run your *current* detector over the overlapping tokens in MELT/SolRPDS and report agreement
   (confusion matrix vs their bundle/rug labels). Disagreements are your highest-value debugging signal.
3. Re-estimate your coordinated-supply numbers and compare to MELT's 36.5% mean. A large systematic
   gap means your clustering is over- or under-linking.

**⚠ Licensing note (you plan to sell — read this).** The Zenodo set is CC-BY-4.0: commercial use is
fine *with attribution*. Academic datasets (MELT, SolRPDS) may carry research-only or share-alike
terms — check each dataset's license before using it to derive a **commercial** product, and keep a
`data/external/LICENSES.md` recording the license and permitted use of every external source. I'm not
a lawyer; if the product is going to market, get the licenses reviewed. Keep a clean provenance chain
from every sellable row back to its inputs so you can prove no restricted data contaminated it.

**Definition of Done.** All three datasets ingested with recorded licenses; a confusion matrix of your
current detector vs MELT/SolRPDS labels; your coordinated-supply estimate reconciled against MELT.

---

## Phase 2 — Reframe the prediction target

**Goal.** The current pipeline conflates two problems that need separating. Structure predicts
*distribution*; it barely predicts *price*. Model them apart.

**Tasks**
1. Split the label into two heads on the snapshot:
   - **`will_distribute`** — team dumps within horizon (structural; your strength; derived from
     `post_grad_behavior.distribution_signal` + exit choreography). This is the *product*.
   - **`will_appreciate`** — MC ≥ Nx within horizon (attention-driven; noisy; needs Phase 5 social
     features to have any hope). Keep it, but treat it as secondary and expect low ceiling.
2. Reformulate the horizons as **discrete-time survival**. Your 1h/4h/24h checkpoints already are a
   survival panel. Fit a discrete-hazard (or Cox) model for *time-to-distribution* that correctly
   handles censoring (coins still alive at 24h are censored, not negatives). This replaces three
   independent classifiers with one coherent model and stops throwing away the alive-at-24h rows.
3. Keep the 4h label as the primary *learning* signal for `will_distribute` only. Do **not** use the
   4h MC-multiple as the primary target for anything structural — at 4h it's mostly momentum noise.

**Definition of Done.** Snapshot carries two label heads + a survival panel; a discrete-hazard model
for time-to-distribution trains and beats the Phase-0 rule baseline on distribution PR-AUC.

---

## Phase 3 — Migrate from hand-tuned rules to a fitted model

**Goal.** Replace magic numbers (the 0.35/0.30/0.20/0.05/0.10 membership weights, the ±2/±1 factor
deltas, every threshold) with weights fit to data. Keep the rules as an interpretable baseline and a
fallback, never delete them.

**Ground-truth strategy: HYBRID (chosen).** Pretrain on external labels (MELT/SolRPDS) to solve the
cold-start problem — at a ~0.2% graduation rate your own distribution-event positives accrue too slowly
to fit a stable model from scratch. Then fine-tune and calibrate on your own freshly-collected
point-in-time data to capture current adversarial drift and your exact feature definitions.
**Strict rule: external data stays on the pretraining side only. Validate and calibrate exclusively on
your own time-split data**, so reported performance reflects live conditions and the pipeline stays
license-clean for any future product. Until your own labeled set is large enough to fine-tune on,
run the pretrained model as a *second opinion* alongside `verdict_rules_v2`, not as the live verdict.

**Tasks**
1. **Freeze the current ruleset as `verdict_rules_v2`** — it stays live and becomes the baseline the
   model must beat in Phase 0's harness.
2. **Per-wallet membership model.** Fit an L1-regularized logistic regression on the `team_members`
   evidence channels against a membership ground truth derived from MELT bundle labels + your
   highest-confidence heuristics. L1 first because it's interpretable and you want to see which
   channels survive. Compare learned coefficients to your hand priors; where they disagree sharply,
   investigate the prior.
3. **Per-token verdict model.** Fit gradient-boosted trees (XGBoost/LightGBM) on the full snapshot
   feature vector against the Phase-2 `will_distribute` head. GBM because the factors interact
   non-linearly (concentration × speed × funder-reputation) in ways a linear score can't capture.
4. **Calibrate.** Wrap the model output in isotonic or Platt calibration fit on a held-out time slice,
   so `confidence` becomes an actual probability. Re-run Phase 0 calibration curve to confirm.
5. **Keep hard-SKIP rules as a safety layer** in front of the model (known-rugger funder, already-dumped,
   etc.) — these are near-deterministic and shouldn't be softened into probabilities. The model replaces
   the *soft factor score*, not the hard gates.

**Definition of Done.** A calibrated `verdict_model_v3` that beats `verdict_rules_v2` on distribution
PR-AUC and Brier score in walk-forward, with a documented coefficient/importance comparison against the
old hand weights. Both run side by side; the ruleset remains the explainable fallback.

---

## Phase 4 — Replace hand-built coordination edges with graph methods

**Goal.** The noisy-OR over fixed edge weights (`funder` 0.90, `same_slot` 0.70, …) is brittle and
gameable. The academic consensus is topological — move there.

**Tasks**
1. Build the per-launch wallet graph (nodes = wallets; edges = shared funder, same-slot co-buy,
   behavioral-fingerprint similarity, lockstep sell). You already collect all the raw edges.
2. Apply **community detection** (Louvain/Leiden) to recover clusters instead of thresholding edges
   one at a time. Compare recovered clusters to MELT bundle labels.
3. Compute **topology features** the literature shows are discriminative and add them to the snapshot:
   - **Star vs Cluster topology** (single core address / batch pattern vs professionalized multi-account
     with division of labor — per SolRugDetector's 78-syndicate analysis).
   - **average degree** and **clustering coefficient** — rug networks show *higher* average degree but
     *lower* clustering coefficient than sustainable ones (ACM Web Science 2025). These are cheap,
     powerful, and hard to fake without changing the underlying operation.
4. Consider **node2vec / graph embeddings** on the cross-coin wallet graph as an upgrade path to your
   9-dim behavioral fingerprint — embeddings generalize across address rotations better than a fixed
   feature vector.

**Definition of Done.** Community-detection clusters replace (or ensemble with) the noisy-OR clusters
and match MELT bundle labels at least as well; topology features (star/cluster, degree, clustering
coefficient) are in the snapshot and show non-trivial importance in the Phase-3 model.

---

## Phase 5 — Add the attention / social layer (largest missing signal)

**Goal.** Your engine is purely on-chain and ignores the single biggest predictor of continuation.
The GRW survival study found launches advertising all three social channels graduate at **17.4×** the
rate of those with none, with a Telegram-alone Cox hazard ratio of **5.40**. You are leaving the
strongest feature on the table.

**Tasks**
1. Add social-presence features to the snapshot (point-in-time as of graduation): Telegram/Twitter/website
   present, follower counts, account age, follower-change velocity.
2. Add **KOL-involvement** features. GMGN already exposes KOL wallet holdings and cluster-buy signals —
   ingest KOL presence/entry-timing as features rather than rebuilding it.
3. Add holder-growth velocity and new-smart-money-entrant rate (you partly capture this in
   `holder_snapshots` — promote it to a first-class feature).
4. Re-run the Phase-3 model with social features and measure the lift on both label heads. Expect the
   biggest gains on `will_appreciate` (the head structure alone can't touch).

**Definition of Done.** Social + KOL + holder-velocity features in the snapshot; measured lift reported
per label head vs the on-chain-only model.

---

## Phase 6 — Adversarial robustness & drift monitoring (make it survive)

**Goal.** Detection targets are actively evaded — 2024-era coordination tooling now emits mechanical
signatures that feed systems filter on sight, and teams randomize the exact signals you rely on
(fresh funders per wallet, staggered timing, CEX-routed funding that reads as "clean"). A static
detector decays. Build for drift.

**Tasks**
1. **Drift monitor** — track feature distributions and model feature-importance over rolling windows.
   Alert when a previously strong feature's importance collapses (a sign teams learned to evade it).
2. **Adversarial red-team doc** — for every edge and factor, write down the cheapest evasion, then query
   recent data to check whether that evasion is already present and rising. Treat rising evasion as a
   tracked feature, not a bug.
3. **Ensemble weak signals; distrust single strong edges.** The 0.90 funder edge is the most gameable
   (multi-hop routing / CEX withdrawals defeat it, and sophisticated teams therefore read as clean).
   Down-weight any single dominant edge and rely on the *aggregate* of many weak, hard-to-simultaneously-fake
   signals (topology + behavioral + timing + concentration).
4. **Scheduled re-fit** — the model retrains on a rolling window on a fixed cadence, and the harness
   auto-compares each new model to the incumbent before promotion. No silent model swaps.

**Definition of Done.** Drift dashboard live; red-team doc committed and refreshed; model promotion
gated by the Phase-0 harness on a rolling schedule.

---

## Phase 7 — Package as a sellable data product  *(DEFERRED — do not build yet)*

**Status.** Deferred while in collection mode. Read it now only so the guardrails you're already
following (versioning, provenance, point-in-time correctness) stay aligned with where this ends up —
that alignment is why they're enforced from day one. Do not build the packaging until the data and
algo are proven.

**Goal (when the time comes).** Decide what you actually sell and make it clean enough to sell. Note the bar: **MELT already
exists** as a public 41k-launch labeled dataset — a buyer's default alternative is free. To sell, you
need what MELT doesn't have.

**What is actually sellable, roughly in order of defensibility**
1. **Real-time, continuously-updated labeled snapshots.** MELT is a static academic drop. A live,
   leak-free, point-in-time feature+label feed updated every graduation is a different product. This is
   your strongest angle.
2. **Exit-choreography labels + graduation-conditional deep microstructure** (first-50-buys slot/bundle
   resolution, per-member exit ordering). This is granular labeling that the public sets don't carry.
3. **Wallet/funder reputation graph** as an enrichment API (cross-token serial-rug linkage).
4. **The calibrated verdict** as a signal feed (lowest defensibility — easiest to replicate, most
   sensitive to the drift in Phase 6).

**Tasks**
1. **Freeze and document the schema** with a versioned, published data dictionary. Buyers pay for
   stability and provenance, not just rows.
2. **Provenance chain** — every sold row traces to its raw inputs and the exact ruleset/model/feature
   versions that produced it. This is also your license-cleanliness guarantee (Phase 1).
3. **Quality SLAs** — freshness (lag from graduation to labeled row), completeness (coverage of
   graduations), and label-revision policy (labels update at 1h/4h/24h — document how revisions are
   delivered).
4. **Reproducibility artifact** — a buyer or auditor can rebuild any historical snapshot from stored
   inputs. This is the difference between a data *product* and a data *dump*.

**Definition of Done.** Versioned schema + data dictionary published; provenance and license chain
complete; freshness/completeness SLAs measured and documented; a historical snapshot reproducibly
rebuilt end-to-end.

---

## Sequencing summary

```
COLLECTION MODE — priority order while accruing data:

Phase 0  Eval harness              ── must be first; everything is judged against its baseline
   +     Collection audit          ── verify max-fidelity, point-in-time, non-recoverable capture (esp. social/microstructure)
Phase 5  Social / attention layer  ── PULLED FORWARD: capture social/KOL state NOW; it's non-recoverable later
Phase 1  External datasets         ── pretraining corpus (hybrid) + benchmark + licensing
Phase 2  Reframe target            ── distribution vs appreciation; survival formulation
Phase 3  Rules → fitted model      ── HYBRID: pretrain external, fine-tune/calibrate on your own data
Phase 4  Graph coordination        ── community detection + topology features
Phase 6  Adversarial + drift       ── make it survive an evolving adversary
Phase 7  Data-product packaging    ── DEFERRED until data + algo proven
```

Note: Phase 5's *feature capture* is pulled forward into collection mode (the state is non-recoverable),
even though its *modeling* payoff lands later alongside Phase 3.

## Reality check to keep pinned

- Graduation base rate is ~0.2–0.26% as of mid-2026 (down ~80% over three months) — positives are
  rare; PR-AUC and calibration matter, accuracy does not.
- Structure buys you **rug-avoidance**, not winner-picking. Market and price accordingly.
- The environment is adversarial and drifting. A model that isn't re-fit and drift-monitored is a
  model that silently stops working. Phase 6 is not optional.

## Key references

- MELT / MemeTrans — arXiv 2602.13480
- SolRPDS — arXiv 2504.07132
- SolRugDetector — arXiv 2603.24625 (Star vs Cluster topology, syndicate analysis)
- Kamat, *Pump.fun Graduation Regime Windows* — SSRN 6915560; dataset Zenodo DOI 10.5281/zenodo.20633486 (CC-BY-4.0)
- *Trust Dynamics and Bot-Driven Responses* — ACM Web Science 2025 (degree / clustering-coefficient discriminators)
