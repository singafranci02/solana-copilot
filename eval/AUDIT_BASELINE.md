# Pipeline audit baseline — 2026-07-13

`uv run python -m eval.audit` — 6 stages, 28 checks, all passing. Run `--quick`
(stages 1-3, ~15s) before deploying any pipeline change; run full (~45s) after any
model/label change. Non-zero exit = do not deploy. History in `backtest_runs`.

Found on its first runs (already fixed): USDC recorded as a graduation with 12k
pre-grad tape rows and 2,379 fake team members; 20 fallback coins still carrying
bloated pre-gate member lists; one stale reached_10x flag.

```

═══ stage 1 DATA ═══
  PASS       v2 graduations have a snapshot (last 7d)                97.8% of 2639
  PASS       no tape rows predate their graduation                   0 of 3,831,868
  PASS       no future created_at                                    0 rows
  PASS       every is_member row passes the gate                     261 violations of 48,153
  PASS       no team exceeds 40 members (bloat regression)           max team size 40
  PASS       tape is_team marks ⊆ cluster members (n=100 sample)     0 mismatched coins

═══ stage 2 LABELS ═══
  PASS       stored trajectories == recompute from tape (n=200)      0.0% differ
  PASS       every reached_10x is sustained (>=3 prints)             0 of 100 violate the sustain rule
  PASS       eval loader excludes thin tapes (<30 prints)            0 thin-tape rows leaked into labels
  PASS       base rate survive60 in [6%,30%]                         15.6% (n=1897)
  PASS       base rate moon10x in [3%,16%]                           10.1% (n=1897)
  PASS       base rate team_exit10 in [40%,80%]                      62.1% (n=1807)
  PASS       base rate rug in [75%,97%]                              89.4% (n=3266)

═══ stage 3 LEAKS ═══
  PASS       rules replay fidelity from frozen snapshots             100.00% of 3832
  PASS       early model has NO pump head (neg. result #1)           heads=['survive60']
  PASS       graduation model uses no post-grad (e5_*) features      leaked keys: none
  PASS       single-feature ROC canary (< 0.95)                      worst 0.664 (bundled_supply_pct→survive60)

═══ stage 4 BACKTEST ═══
  PASS       distribute ROC in [0.88, 0.97]                          0.923 (n=1964) — in band
  PASS       rug ROC in [0.84, 0.96]                                 0.860 (n=1960) — in band
  PASS       survive60 ROC in [0.7, 0.9]                             0.750 (n=1139) — in band
  PASS       team_exit10 ROC in [0.66, 0.86]                         0.736 (n=1085) — in band
  PASS       moon10x ROC in [—, 0.68]                                0.565 (n=1139) — in band

═══ stage 5 ALERTS ═══
  PASS       pre-warn precision @p>=0.9 >= 85%                       89.7% on 184 fires of 1085
  PASS       pre-warn fire rate in band                              17%
  PASS       exit alarm: median 1h-after multiple <= 0.5             0.23x (n=669)
  PASS       exit alarm: better off exiting >= 75%                   84%

═══ stage 6 CALIBRATION ═══
  PASS       calibrated p_rug beats base-rate Brier                  model 0.0472 vs base 0.0597
  PASS       per-decile |predicted-realized| <= 0.20 (n>=30 bins)    worst gap 0.025

================================================================================
28/28 checks passed (43s, mode=full)
```
