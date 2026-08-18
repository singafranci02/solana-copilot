# solana-copilot — project context for Claude

## What this is

A self-learning Solana memecoin analyst that silently tracks teams, funders, and
early buyer clusters. It produces structural reads on tokens — never trades.

**Do NOT implement trade execution. This is analysis only.**

## Scope: classic pump.fun ALERTS; classic + Mayhem DATA (owner decision, 2026-08-17)

Pump.fun's **Mayhem mode** (its own enhanced-launch mode, program
`MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e`) carried ~90% of graduation flow and
its teams behaved statistically identically to classic launches — but the owner
decided to target classic pump.fun only. All Mayhem history was purged (backup at
`db/pre_mayhem_purge.backup.db`); the live gate skips Mayhem creations on-chain
(creation tx contains the MAyh program). Consequences to keep in mind:

**UPDATE 2026-08-17 — Mayhem is collected again, as data only.** Measured against
an independent graduated feed, **89 of 100 pump.fun graduations are Mayhem-mode**,
so classic-only had fallen to ~20 coins/day and left every model head below the
500 rows needed to make any claim. Mayhem coins are now analysed and stored with
`platform='mayhem'`, forming a SEPARATE population:

- `eval._common.load_samples(conn, platforms=...)` selects CLASSIC (the default),
  MAYHEM, or BOTH. Existing callers keep classic; combining is explicit.
- **Mayhem never reaches a recommendation.** Both coin-level alerts gate on
  `platform == 'pump.fun'`, and the audit asserts no alert has ever fired for a
  non-classic coin and that the classic training population contains only classic.
- foreign launchpads (rapidlaunch, bonk.fun, ...) remain excluded and purged —
  Mayhem is not foreign, it is pump.fun's own mode.
- the pre-purge backup is still `db/pre_mayhem_purge.backup.db`; post-purge Mayhem
  history was destroyed, so the new population accumulates from 2026-08-17.

Remaining consequences of the original decision:

- classic flow is ~20 graduations/day, so the classic-only samples grow slowly
- every pre-purge metric (ROCs, precisions, base rates) was measured on the MIXED
  population and must be re-measured before being quoted
- model heads below their n-gates stay untrained until classic data accumulates

## Manufactured graduations (flagged, kept, excluded from training)

A "manufactured" graduation = one entity/bundle buys the curve to force migration
("one big vertical line"). Detection: >=2 independent flags among lightning curve
(<5min creation->graduation), <25 BC buyers, top-5 buyer share >=60%, team supply
>=50%, same-slot bundle >=8 (src/analyzer/manufactured.py). ~9% of classic coins.
Policy — deliberately different from the Mayhem purge: they ARE classic pump.fun, so
they stay in the DB and the live pipeline (recognising one and skipping it is product
value; alerts carry an annotation), but they are EXCLUDED from every model/label
population — their tape is one entity's puppet show, not price discovery.

## Main goal (owner, 2026-08-13)

**Make money.** Everything below serves that. The system is not an academic
exercise — it is a proprietary dataset nobody else is collecting, and the two
routes to monetising it are (a) trading the structure it reveals and (b) tooling
that helps coins launch well and not rug. Keep both live when weighing work.

### The cycle question — the current research frontier

Most teams dump. That much is settled (96.9% rug, median first sell 29s). The
open and more valuable question is what they do AFTER the first dump:

> do teams sell into strength, re-buy cheaper, and push the coin up again —
> and can that cycle be detected early enough to act on?

First measurement (2026-08-13, 391 anchor-gated coins): **38.9% of coins show the
team selling then re-buying >=10% lower**, 2,795 round-trips, median discount
captured **32.4%**. Price after the rebuy reached a median 1.56x within 10 min
(51.7% >=1.5x, 33.8% >=2x). CAVEAT: those lift figures used raw MAX price and are
UPPER BOUNDS — they must be re-run under MIN_TRADES_AT_PEAK before being quoted,
because 78% of raw extremes in this tape are single bad prints.

Two things this does NOT yet establish, and both gate any trading use:
- the rebuy is chosen by the team, so the forward move is conditional on their
  intent — it is not yet shown to be a signal an outsider can act on
- it has not been tested whether a rebuy is detectable in time to follow

What is legitimate to build from this: detection of the cycle (product value —
recognising a managed distribution is worth as much as recognising a rug), and
launch tooling that makes distribution transparent and pre-committed. What is not:
tooling that times insider sells to avoid detection. That line is about
concealment, not about analysis.

## Core strategic focus: graduation-first analysis

~99.3% of Pump.fun tokens never complete their bonding curve. The system focuses
exclusively on the ~0.7% that **graduate** (raise ~85 SOL → auto-migrate to PumpSwap
at ~$69K market cap). Graduation is the primary quality filter.

The core question at graduation: **is the team/early cluster about to distribute,
or does the structure support continuation?**

## Data flow

```
Pump.fun WebSocket
    ├── newCoinCreated → pump_monitor.py    (60s collection, BC-phase analysis)
    └── migrate        → graduation_monitor.py  (structural analysis, this is primary)

graduation_monitor.py:
    → fetch top holders from Helius at graduation moment
    → build team cluster (who accumulated during BC + still holds at graduation)
    → identify funder wallet (one hop back from team members)
    → produce StructuralRead verdict (SKIP / WATCH / STRUCTURALLY_SOUND)
    → schedule distribution checks at +1h / +4h / +24h
    → update funder_reputation and wallet_stats after 4h outcome
```

## Self-learning loop

The system learns purely from its own observations — no external APIs for win rates.

1. Outcome tracker checks price at 1h / 4h / 24h from graduation
2. Classifies: moon (≥3× graduation MC) / ok (0.5-3×) / rug/dead (<0.5×)
3. Updates `wallet_stats` incrementally (wins/losses/total_calls)
4. Updates `funder_reputation` incrementally (rug_rate, moon_rate)
5. `is_known_rugger` is set ONLY when funder has ≥8 graduated mints AND rug_rate ≥ 0.65

## Verdict rules (structural_read in rules.py)

Hard SKIP (checked first):
- Funder is a known rugger (is_known_rugger=True, requires n≥8 sample)
- Distribution signal is DUMPED
- Team holds ≥50% supply at graduation AND is a BC sniper

STRUCTURALLY_SOUND: positive score ≥2 with no negative overrides
  +2 smart money count ≥2
  +1 smart money count = 1
  +2 distribution signal = ACCUMULATING
  +1 distribution signal = HOLDING
  +1 team supply_pct < 20%
  +1 funder has moon_rate ≥ 40% with ≥8 sample

WATCH: everything else (insufficient signal or mixed)

## What the system can and cannot predict

Read `eval/NEGATIVE_RESULTS.md` before proposing a new signal. In short:

**Works** (the surviving claim, adversarially audited): the **30s exit alarm** —
alarming on the top 20% of p_exit gives 69-71% precision against a ~40% base rate,
**lift +29.7%, 95% CI [+15.9%, +43.6%]**, P(no effect) 0.00%. This is the deployment
gate and the only validated predictive result.

**Retired, with the reasons** — the old headline numbers do not survive scrutiny:
- *team will distribute ROC 0.937* — mostly mechanical. The label needs the team to
  shed >30% of supply, so below 30% it is unreachable (0.4% labelled vs 70.8%
  above). Where the question is genuinely open it scores **0.618**. NEGATIVE_RESULTS #20.
- *coin will rug ROC 0.912* — base rate saturated to 96.9%; now ~0.64 with ~17
  negatives in 582 coins. NEGATIVE_RESULTS #16.
- *survives ≥60min* and *team_exit10* — saturated at 5.6% and 96.5% base rates.

Quote **lift over the base rate**, never absolute precision or win rate: a 94.2%
precision alarm was 2.3 points WORSE than never alerting (#18), and a 55.4% win-rate
strategy had a 0.963 mean (#19).

**Does not work — do not retry without a new argument:** the **10× is unpredictable**,
from graduation structure (ROC 0.583) *and* from early order flow (0.592). An early pass
appeared to hit 0.746 but that was `price_run` leaking the label — 36% of 10× coins hit
10× inside the 5-minute window. Corrected, it is 0.517: a coin flip.

That negative result also **cancels the planned social/attention layer**. On-chain crowd
arrival is a direct, unfakeable, free measurement of attention, and it fails to predict
the pump; a paid follower-count proxy for the same quantity will not do better.

Never add a moon/10× head. Anything that only fires once the pump is visible in the price
is **detection, not discrimination**, and has no value.

## Team membership gate (team_detect.py)

The membership score alone over-included badly (avg 86 "team" wallets/coin, max 628):
additive weak evidence — a same-slot edge + early-buyer + fresh wallet — crossed the
0.35 bar with no team-specific fact. Ground-truthed on the tape, those edge-carried
members were 9.8% insiders (75% never sold); buyer∩holder members with corroboration
were 26.7%. `passes_member_gate` therefore requires skin in the game: coordination
edges CORROBORATE membership, they never CARRY it. Trajectory labels also require
`n_price_points >= 30` — a thin tape misses the collapse and fakes a survivor.

## Pattern significance thresholds

Every PatternResult carries `sample_size` and `is_significant` (True only when n≥30).
Patterns below threshold must NOT feed automated warnings. They are hypothesis-level
output only. Enforce in code — never assert significance without checking the flag.

## Classification thresholds

The 1h/4h/24h checkpoints in `outcome_tracker.py` (moon ≥3×, rug <0.3×) are LEGACY.
They still feed the `wallet_stats` / `funder_reputation` counters, but they are not
the labels the model learns from, and 1h is far too late to measure anything: on our
own tape the **median coin collapses at 10.5 minutes** and 89.6% are dead within the
hour. Checking first at 1h was measuring the corpse.

The real labels come from the swap tape, in `src/analyzer/trajectory.py`:

| Label            | Condition                                                    |
|------------------|--------------------------------------------------------------|
| collapse         | price < 0.5× the first post-graduation print                 |
| moon (`reached_10x`) | ≥10× — **sustained**, confirmed by ≥3 prints at the level |
| team exit        | first sell by a GATED team member (median 2.4 min; leads the collapse 80% of the time) |

`MIN_TRADES_AT_PEAK = 3` is not optional: **78% of raw ≥10× maxes were single bad
price prints** (one coin printed 2055× on one trade; its true peak was 1.11×). Without
the sustain rule the 10× rate reads a fake 26% instead of the true ~9%.

Live checks run at **EARLY_CHECK_SECONDS** (120,210,300,390,480,600,720,900,1200,2400s\n— every value is a v5 hazard-grid edge), then 1h / 4h / 24h.

Distribution signal thresholds (distribution.py):
- DUMPED:       holders < 5
- DISTRIBUTING: team sold > 30% of graduation-time position
- ACCUMULATING: team grew position by > 10%
- HOLDING:      everything else (including unknown)

## Key constants (verify before trusting)

```
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"  # TODO: verify
GRADUATION_EVENT    = "migrate"    # Pump.fun WebSocket event name — TODO: verify
GRADUATION_SOL      = ~85 SOL      # raised on bonding curve
GRADUATION_MC_USD   = ~$69,000     # at migration
```

## Database tables

| Table                | Purpose                                             |
|----------------------|-----------------------------------------------------|
| tokens               | All analysed tokens                                 |
| wallets              | Wallet registry with smart_money_score              |
| wallet_stats         | Incremental win/loss counters (min 15 for win_rate) |
| token_buyers         | BC-phase purchase records                           |
| wallet_clusters      | Legacy BC-phase funding clusters                    |
| team_clusters        | Graduation-context team clusters with supply_pct    |
| coin_outcomes        | Price snapshots at 1h / 4h / 24h                   |
| graduation_events    | Graduation records with BC top holders              |
| post_grad_behavior   | Distribution checks at 1h / 4h / 24h               |
| funder_reputation    | Funder track record (min 8 for is_known_rugger)     |
| team_fingerprints    | Legacy team fingerprints (pump_monitor era)         |
| cex_hotwallets       | Known CEX hot wallets (seeded + DB-extended)        |
| narratives           | Active narrative tracking                           |

## Services (launchd on Mac mini)

| Service             | Entry point                          |
|---------------------|--------------------------------------|
| pump_monitor        | src/services/pump_monitor.py         |
| graduation_monitor  | src/ingest/graduation_monitor.py     |
| wallet_watcher      | src/services/wallet_watcher.py       |
| narrative_tracker   | src/services/narrative_tracker.py    |
| analyzer_server     | src/services/analyzer_server.py      |

## CEX wallet handling

CEX-funded wallets are excluded from clustering. Seed list is in
`src/common/cex_wallets.py`. Extended via `cex_hotwallets` DB table.
Use `is_cex_wallet(address, conn)` everywhere — never hardcode CEX checks.

## Development rules

- Read the current file state before editing — never assume content from memory
- Match existing code style (type hints, docstrings only for non-obvious WHY)
- No trade execution — analysis output only
- Run `uv run pytest` before committing
- Run `uv run python -m eval.audit --quick` before deploying pipeline changes, and the
  full audit after any model/label change — non-zero exit means do not deploy
- Schema migrations go in db/schema.sql only (CREATE TABLE IF NOT EXISTS throughout)
- `uv` binary at: `/Users/francescotomatis/Library/Python/3.13/bin/uv`
