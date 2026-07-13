# solana-copilot — project context for Claude

## What this is

A self-learning Solana memecoin analyst that silently tracks teams, funders, and
early buyer clusters. It produces structural reads on tokens — never trades.

**Do NOT implement trade execution. This is analysis only.**

## Scope: CLASSIC pump.fun launches only (owner decision, 2026-07-13)

Pump.fun's **Mayhem mode** (its own enhanced-launch mode, program
`MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e`) carried ~90% of graduation flow and
its teams behaved statistically identically to classic launches — but the owner
decided to target classic pump.fun only. All Mayhem history was purged (backup at
`db/pre_mayhem_purge.backup.db`); the live gate skips Mayhem creations on-chain
(creation tx contains the MAyh program). Consequences to keep in mind:

- classic flow is ~10-40 graduations/day, so samples grow ~10x slower
- every pre-purge metric (ROCs, precisions, base rates) was measured on the MIXED
  population and must be re-measured before being quoted
- model heads below their n-gates stay untrained until classic data accumulates

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

**Works** (leak-audited, out-of-time): team will distribute ROC **0.937**; coin will rug
ROC **0.912**; survives ≥60min ROC **0.806** from graduation structure, **0.904** from
order flow at T+5min (top-5% survive 100%).

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

Live checks run at **5 / 10 / 20 / 40 min**, then 1h / 4h / 24h.

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
