# Negative results

Things we tested, that did not work, and must not be quietly retried. Each one cost
real time; the point of writing them down is that they only cost it once.

---

## 1. The 10× pump is not predictable. From anything we have.

**Hypothesis:** graduation structure can't see the pump because the pump is a *crowd*
phenomenon, and the crowd hasn't arrived yet at T+0. Measure the crowd 5 minutes later
(order flow: distinct wallets, arrival acceleration, retail net inflow) and the 10×
becomes predictable.

**Result: FAILED. Twice, independently.**

| predicting `reached_10x` from | ROC | note |
|---|---|---|
| structure @ graduation (T+0) | **0.583** | coin flip |
| early order flow @ T+5min | 0.746 | ⚠️ **LEAKY — not a result** |
| early flow, `price_run` removed | 0.623 | |
| early flow, only 10× still FUTURE at T+5m | 0.592 | |
| early flow, **both corrections** | **0.517** | coin flip |

**The trap, in detail.** The first pass looked like a win (ROC 0.746, top-5% picks 10×
44% of the time, a 5.7× lift). It was not. `price_run` = peak/first *within the window*,
and **36% of coins that reached 10× did so inside the first 5 minutes** — so for a third
of the positives the feature literally contained the label. The model had learned
"is it already at 10×?", which is a question with no value: by the time it fires, the
move is in the price.

This is the exact failure the north star warns about — **detection, not discrimination.**
A pump detector that only lights up once the pump is visible is a chart with extra steps.

**The two corrections that expose it:**
1. drop `price_run` (kills the direct label channel),
2. drop the coins already at 10× by minute 5 (asks the only question worth asking:
   *will it 10× from HERE?*).

Apply both and it is 0.517. There is no signal.

**Therefore: never add a moon/10× head to `early_attention.py`.** The docstring says so;
this is why.

---

## 2. …which also settles the social/attention layer. Don't build it.

The standing plan was a Twitter/Telegram follower-velocity layer, on the theory that
attention drives pumps and we weren't measuring attention.

We *are* measuring attention — better than any social API can. Crowd arrival in the order
flow (`n_wallets`, `accel`, `new_wallet_rate`, `retail_net_sol`) is attention that has
already **converted into money**: direct, unfakeable, free, no API key, no rate limit.
Follower counts are a lagging, botted, gameable *proxy* for it.

The direct measurement does not predict the pump (§1). Paying for a worse proxy of a
quantity that already failed is not a plan, it's a purchase. **Deferred indefinitely**,
and it needs a new argument — not a new vendor — to come back.

---

## 3. Network topology of the buyer graph adds nothing.

Freeman centralization, average degree, clustering coefficient, Louvain community counts,
rebuilt point-in-time from same-slot co-buys / shared funders / near-identical buy sizes.

Topology **alone** predicts rug at ROC 0.78–0.80 — genuinely informative. But added to the
existing feature set it moves the model **not at all**. It is *redundant*, not useless:
the coordination engine and funder-reputation features already carry the same information,
in a form the model can use more directly. Not shipped. See `eval/topology.py`.

---

## 4. Isotonic calibration degrades the rug head.

Under a ~91% base rate, PAV/isotonic has too few negatives to fit against and overfits the
tail: rug ROC **0.804 → 0.752**. **Platt scaling** is what works here (and is what ships).
Don't "upgrade" the calibrator without re-measuring.

---

## What DOES work (for contrast)

The rug is extremely predictable. That is the whole product.

| target | ROC | leak-audited |
|---|---|---|
| team will distribute | **0.937** | ✅ |
| coin will rug | **0.912** | ✅ |
| survives ≥60min, from structure @T+0 | **0.806** | ✅ dropping `price_run` doesn't degrade it |
| survives ≥60min, still-alive coins @T+5m | **0.904** | ✅ top-5% survive **100%** |

The user's own framing was right all along: *"every coin is a rug — it's all about finding
out when the rug is coming."* That question we answer well. The 10× is a different game,
and we have no edge in it.

---

## 5. There is no profitable BUY signal. Not even gated on the models that work.

**Hypothesis (worth testing, and tested properly):** some teams *rug* — they kill the
coin — while others *distribute skillfully*: they push the price down, the coin absorbs
it and recovers. Separate the two, buy the dip after a skillful dump.

**The first half is TRUE.** After a team's first sell, a bounce genuinely exists: 39%
of coins hit a sustained +50% within the hour, and 14% are *higher* an hour later. It
is not a uniform death spiral.

**The second half is FALSE. Every entry rule loses money.**

Buy 5 min after the team's first sell, sell at first sustained +50%, else hold 1h,
3% round-trip costs:

| gate | mean return |
|---|---|
| no gate | **0.885×** (−11.5% per trade) |
| gated: top-10% survival score | 0.822–0.865× |
| gated: top-5% survival score | 0.858–0.983× |

Entering at T+5min post-graduation instead (the natural use of the survival model),
across take-profits of 1.3/1.5/2/3× and gates of top-20/10/5%: **every single cell is
below 1.00.** The most favourable one bootstraps to a 95% CI of **[0.690, 1.063]** —
91% probability the true edge is at or below break-even.

**Why it fails even though the model works.** The survival head is genuinely strong:
top-10% picks survive ≥60min 43% of the time against an 11% base — a real 4× lift.
**Surviving is not the same as going up.** The coins that live simply bleed slower. The
bounce is real but too small and too unreliable to cover fees plus the 55% of trades
that bleed out.

Discrimination is real. Long-side profit is not. Do not rebuild this as a buy signal.

**What this leaves — and it is genuinely valuable.** Invert it. For someone *already
holding*, the team-exit alert is a real exit signal (n=1690):

| | |
|---|---|
| price 1h after the team's first sell (median) | **0.23×** |
| P(you are better off exiting on the alert) | **86%** |
| median value preserved by exiting | **77% of position** |
| P(you'd have gained >20% by holding) | 9% |

The product is **risk and exit**, not entry. Value = losses avoided, not alpha captured.
Say that plainly; anything else is a claim the tape does not support.

---

## 6. Sell *structure* discriminates the bounce — but the payoff geometry still kills the trade.

**Hypothesis:** a team that exits in an orderly ladder (even clips, staggered, then stops)
is distributing, not rugging — and the coin bounces. Buy that, skip the panic dumps.

**The behavioural claim is REAL.** Out-of-time on the 0–15min window (n=940), sell
structure predicts a sustained ≥2× bounce at ROC 0.616, and the hand-built archetype
separates cleanly:

| 0–15min archetype | n | bounce rate | mean return |
|---|---|---|---|
| **ORDERLY** (even clips + team finished + market absorbing) | 68 | **29.4%** | 0.960× |
| PANIC (erratic clips + still selling at the end) | 131 | 24.4% | 0.933× |
| everything else | 741 | 17.9% | 0.858× |

Single strongest reads (low vs high tercile): **fewer team sellers → 29.1% bounce vs
11.3%**; team sells early and stops → 27.9% vs 13.4%.

**The trade is still dead, and this time the reason is arithmetic, not data.**

    when it bounces : you capture  +46%
    when it doesn't : you eat      -41%
    => BREAK-EVEN HIT RATE NEEDED  = 47.1%
    best hit rate any slice reached =  29.4%

The payoff is symmetric-ish while the hit rate caps near 29%. **Even a perfect
sell-structure model would have to more than double the best observed hit rate just to
reach zero.** Better features cannot close a gap this shape.

Letting winners run doesn't rescue it either. A first pass showed "mean 4.26×" — that was
**one 1115× print** (the single-bad-print artifact, for the third time). With executable
fills (robust median entry AND exit), every rule loses: median **0.68×**, P(profit) 8–25%,
and stripping the single best trade drops the mean to 0.79×.

**Kept:** the sell-structure features are excellent *risk* signals (many team sellers =
coin is dead). They feed the exit alarm's severity. They must never feed an entry signal.

---

## 7. "Is it a real project (website/X)?" does not predict anything. It is slightly INVERTED.

The `project_classifier` (heuristics + local LLM, already live) has labeled 265 projects
and 1,830 memes that also have a trajectory:

| | PROJECT | MEME |
|---|---|---|
| survives ≥60min | **16.2%** | **20.1%** |
| reached 10× | 6.0% | 9.7% |
| peak ≥2× | 34.7% | 42.0% |
| median peak | 1.57× | 1.70× |

Real projects do **worse**, not better (z = −1.58 on survival — not significant, but the
point estimate is the wrong way round for the hypothesis, and it is nowhere near a lift).

A deeper LLM agent that visits the site and grades quality/novelty is a *finer* instrument
than this binary label — but it now carries the burden of proof, because the coarse version
shows nothing and if anything leans negative. Memecoins pump *because* they are memes.
Do not build the agent as a signal without first showing quality separates outcomes
*within* the 345 already-labeled projects.

---

## 8. Team prior exit tempo adds nothing on top of existing features.

Hypothesis: gated team members' exit speed on their PREVIOUS coins predicts this
coin's exit (behavioral tempo persistence). Point-in-time safe, 81% coverage.

Alone: ROC 0.605 — the persistence is real. Added to the current feature set:
0.739 → 0.739, exactly zero lift. Funder reputation, team scores and the wallet
graph already carry the operator-history information. Tested before building
(the right order); not shipped.

---

## 9. The 2× entry fails too — even though 2× is common and the selector works.

**Hypothesis (user's, and the most reasonable one yet):** forget the 10×; aim for
sustained ≥2×, which is common. Predict which teams intend to let the coin run, enter
at graduation, exit at 2× before the team decides.

**What's true in it:** sustained 2× happens on **42.9%** of coins. Structure at T+0
predicts it at ROC 0.628 (top decile hits 57.5%, a 1.4× lift, folds stable ±0.015).
Team *intent* is even faintly visible: "team will let it run to 2× before selling"
scores ROC 0.668 (1.9× lift), "to 1.5×" ROC 0.646 (2.2× lift).

**Why it still loses money — honest fills, 3% costs, every variant:**

| top-20% gate, TP 2× | mean | TP hit |
|---|---|---|
| stop −50%, hold 1h | 0.890× | 23% |
| no stop, hold 1h | 0.795× | 26% |
| no stop, hold 4h | 0.735× | 28% |
| no stop, hold **12h** | 0.730× | 29% |

Best cell across all gates/TPs: 0.901×, 95% CI [0.800, 1.010], **96.5% probability the
true edge ≤ break-even**. Break-even needs a ~41% hit; the ceiling is 29%. The gap
between the 57.5% *label* hit and the 29% *executed* hit is path and execution: entry
slippage in a fast open, the 2× must be reachable from YOUR entry with 3 confirming
prints inside the hold, and the losers bleed to 0.23×.

**The mechanism, from the revealed-target data (n=1,801):** the median team's revealed
target is **1.07×** — 69% dump below 1.2×, only 8.6% let it run to 2× before their
first sell. There is no room between entry costs and the team's exit because for two
thirds of teams the plan IS the instant exit. Most 2× moves (43% any vs 8.6% pre-exit)
happen *through* the team's selling, crowd-driven — magnitude remains unpredictable
(see #1).

This is the **fifth** independent falsification of a long-side entry (10×, early
attention, dip-buy, sell-structure, 2×). The long side is closed. The intent heads are
NOT shipped — ROC 0.63–0.67 with negative economics is research, not product.

---

## 10. Short-memory / recency-weighted training: no benefit YET (data too shallow).

Raced expanding-window (current) vs sliding windows (7/14d) vs exponential-decay
sample weights (half-life 3/7/14d) on identical out-of-time folds:

| scheme | rug ROC | team_exit10 ROC |
|---|---|---|
| expanding (current) | 0.860 | 0.733 |
| best decay variant | 0.864 | 0.736 |

Differences are ±0.004 — noise. The labeled v2 dataset spans only ~10 days; there is
no "long ago" to forget. **Not a permanent negative**: re-run this race when the
dataset spans 60+ days (the weekly retrain keeps a log; the drift monitor watches
decay). Until the race shows a real gap, recency weighting is complexity without
benefit. What "learns with time" actually means today: weekly automated retrain
(each one re-derives the alert threshold) + entity-level memory (funder/wallet
reputations), which is already long-memory and now clean.

## 11. (Positive, but not tradeable) Team behavior PERSISTS across coins.

"If a funder's team HELD (no exit ≤1h) on coin k, what happens on coin k+1?"

| | next team holds | next coin survives 1h |
|---|---|---|
| prior team HELD (n=109 funder pairs) | **14.7%** | 18.3% |
| prior team DUMPED (n=1,092) | 8.7% | 11.9% |
| wallet level (n=12,680 pairs) | **15.4% vs 8.6%** (z≈7.6) | ~flat |

Holding teams are ~1.7–1.8× more likely to hold again — real, and wallet-level highly
significant. But the absolute rate is the story: **even proven holders dump their next
coin 85% of the time.** Keep the memory (free — funder_reputation + team_members
already store it), surface it as an annotation ("this team held its last coin — rare"),
but it is NOT a buy signal and (like prior-tempo, #8) almost certainly adds nothing as
a model feature on top of funder reputation. Do not build more than the annotation.

---

## 12. (Drift, not a negative) Classic pump.fun accelerated — survival fell to ~6%.

Measured 2026-08-02 on verified-classic dense coins (n=334): 60-min survival is now
~4-6% (full-tape-only 5.9%), down from the 15.7% of the old MIXED population, and the
median coin collapses at **5.8 min vs 10.5 min** historically. The later half of the
timeline survives just 1.2% (partly recency censoring — recent survivors are still
ungradable while recent deaths are already labeled — but the full-tape number confirms
the level is genuinely ~6%).

This is real market drift toward brutality, NOT a labeling regression (collapse stamps
are sane, single-print artifacts already killed). The survive60 base-rate band floor
was re-anchored 0.06 -> 0.02 to reflect the classic population; the upper bound stays
0.30 as the thin-tape fake-survivor guard. The v5 hazard model is the right instrument
for this regime — a faster market makes the EARLY hazard (first 30s-5min) more, not
less, the whole game.

---

## 13. (Drift cont'd) The legacy v4 heads are aging out — v5 is their replacement.

Second re-anchor in a week (2026-08-05). The market acceleration in #12 propagated into
the fixed-4h-checkpoint heads: distribute ROC rose to ~0.97 (team structure is nearly
deterministic of the 4h outcome in a market that dies in minutes), rug ROC fell to ~0.79
and its calibration can no longer beat a constant (91-97% of coins rug — a saturating
base rate leaves no headroom). VERIFIED not a leak: the blocking single-feature canary
passed (worst = team_supply_pct 0.939 < 0.95, and that IS the thesis, not a leaked label).

Meaning: a 4-hour distribution/rug label on a coin dead at 5.8 min is measuring a corpse
— the same failure mode the project already fixed once by moving from 1h checkpoints to
trajectory labels. The v4 heads are now DIAGNOSTICS, not the product; their audit bands
are allowed to track drift while the blocking guards (single-feature canary, data
integrity, replay fidelity) stay strict. The product is the v5 competing-risks hazard
model, which measures the early window continuously — exactly where a faster market puts
all the signal.

---

## 14. "Buy the dip, but condition on WHO buys the recovery" — the signal is real, the trade still loses.

**Hypothesis (owner's, and a genuinely new variable):** #5 falsified dip-buying after a
team dump, but never asked *who buys it back*. If the TEAM re-accumulates that is a
different animal from random retail catching a knife — so gate the dip trade on buyer
identity.

**The descriptive claim is TRUE.** Entry = 5 min after the team's first sell, buyers
categorised inside that window (n=378 coins, robust prices, no look-ahead):

| buyer signal (top vs bottom quartile) | median forward peak | P(reaches 2x) |
|---|---|---|
| **team buys back** | 1.22x vs 1.13x | **26% vs 16%** |
| team share of buy SOL | 1.17x vs 1.13x | 18% vs 16% |
| OTHER teams' wallets buying | 1.19x vs 1.10x | 16% vs 15% |

So team re-accumulation genuinely marks a fatter right tail (~1.6x lift on P(2x)).
Other teams' wallets buying carries NO signal — that part of the hypothesis is dead.

**The trade is still unprofitable, and gating makes the typical outcome WORSE:**

    buy 5min after team's first sell, TP at sustained 2x, 30min cap, 3% costs
      no gate            mean 0.801x   median 0.752x   P(win) 24%
      team rebuy > med   mean 0.753x   median 0.639x   P(win) 23%
      team rebuy top 25% mean 0.766x   median 0.601x   P(win) 25%
      bootstrap CI [0.647, 0.894]   P(true edge <= break-even) = 100%

The paradox is the finding: team rebuy predicts the TAIL but degrades the BODY. Coins
the team buys back are more volatile in both directions — more 2x, and a worse median.
Selecting for them buys lottery tickets at a worse average price.

Sixth independent falsification of a long-side entry (10x, early attention, dip-buy,
sell-structure, 2x, and now buyer-identity-gated dip-buy). The long side stays closed.

**Useful by-product — what "dead" actually means** (confirmed-collapse coins, robust
sustained prices, n=361): 28% never sustainably regain half their opening price; the
median best SUSTAINED price after a confirmed collapse is 0.68x the opening anchor and
p90 is 2.79x. So a collapsed coin usually keeps trading — it just rarely gets back to
where it started. "Dead" is better defined as "never sustainably recovers half" than
"stops trading".

---

## 15. THE STRUCTURAL ANSWER: post-graduation drift is negative and nothing escapes it.

After six falsified entry rules, the right question stopped being "which signal?" and
became "what does the price path actually do?" Measured on dense-tape classic coins,
entry at T+10s (a realistic race lag), robust 3-print prices, n=271-277:

| hold from T+10s | median return |
|---|---|
| 20s  | 1.000 |
| 45s  | 0.999 |
| 60s  | 0.994 |
| 5 min | 0.970 |
| 15 min | **0.676** |
| 30 min | **0.455** |

**The median coin loses half its value within 30 minutes of graduation.** There is no
holding period with positive median return, and P(price > entry) never exceeds 42% at
any horizon. The "graduation pop" is already over before T+10s — i.e. it belongs to the
MEV/bot race, not to anyone acting on data.

TWO GATES TESTED AGAINST IT, BOTH FAIL:
- exiting BEFORE the team (coins whose team exits >120s): 60s median 1.000x — no better
- the v5 model's OWN out-of-time danger score, safest decile: 0.970 / 0.833 / 0.564 at
  5/15/30 min. The best selection the system can make still loses 44% in half an hour.

WHY EVERY LONG ENTRY FAILED, IN ONE SENTENCE: a long position must overcome a ~-55%/30min
median drift using a right tail that is measured-unpredictable (#1, #9). That is
arithmetically hopeless, and it explains #5, #6, #9 and #14 as one phenomenon rather
than four coincidences.

WHAT THIS MAKES THE DATA GOOD FOR — the same fact inverted:
- the negative drift is RELIABLE, which is precisely why exit timing pays (median 88%
  of position preserved by acting on the team-exit alarm)
- avoidance has real value: rug prediction at ROC 0.91 tells you what not to touch
- shorting would monetise the drift directly, but new memecoins have no borrow market,
  so it is not accessible

CONCLUSION: this dataset makes an excellent RISK instrument and a hopeless ENTRY
instrument, and that is a property of the asset class, not a limitation of the model.
Do not test another long-entry variant without first showing the median drift has
changed sign.

---

## #16 — the rug head is dying of base-rate saturation (2026-08-11)

The "will it rug" head measured ROC 0.912 when the population still contained
survivors. On the current anchor-gated classic population it reads **0.636** at a
base rate of **97.1%** (n=581), and the audit band [0.74, 0.96] now fails.

This is NOT the label-anchor bug fixed the same day — that one inflated survival by
counting unobservable events as non-events, and it was corrected before these
numbers were taken. This is the opposite and it is real: **97.1% of anchor-valid
classic graduations now rug.** There are ~17 negatives per 581 coins left to
discriminate, so the question "will this rug?" has almost no information content,
and ROC estimated on that many negatives is noisy besides.

Do not fix this by re-anchoring the band. A band exists to detect exactly this, and
moving it to accommodate the reading would delete the finding. Two honest readings:

- as a SIGNAL the head is finished — "yes" is right 97.1% of the time without a
  model, so no threshold on it can pay for its own complexity;
- as a MEASUREMENT the saturation is itself the product. A market where 97.1% of
  graduations rug is a different market from the one where 89% did, and the drift
  is worth tracking as a regime indicator.

Consistent with #15: the entry side keeps getting worse while the risk side keeps
being reliable. Retire the rug head from scoring before adding anything to it.

---

## #17 — candlestick shape: real reflexivity, no tradable content (2026-08-11)

Hypothesis worth taking seriously: traders act on what they SEE, so the visual
shape of the chart is causal, and reading candles should add information. Tested
properly rather than dismissed — bars built from the stored tape at zero API cost,
features strictly from bars <= t, targets strictly after t, out-of-time split with
coins never spanning train and test. 196 coins, 38,960 bar-observations.

**Finding 1 — there is no chart to read before the dump.** Median 18 trades and
21 seconds elapse before the team's first sell. 41.7% of coins have fewer than 10
trades of history at that moment; only 6.5% have five or more 1-minute bars. Chart
reading cannot anticipate a dump that happens before a chart exists. Only 20 of 309
coins (6.5%) exit later than 5 minutes.

**Finding 2 — chart features DO predict retail behaviour.** Predicting top-quartile
retail buy volume over the next 2 minutes: baseline (price level + elapsed time +
recent return) ROC 0.727, adding chart features 0.758. Coin-bootstrapped delta
+0.030, 95% CI [+0.009, +0.053], P(no effect) 0.2%. The reflexive channel is real.

**Finding 3 — but the content is ACTIVITY, not geometry.** Leave-one-out on the
test ROC attributes the gain to trade count (+0.0122), volatility (+0.0067) and
volume surge (+0.0023). The features a human eye actually reads contribute nothing:
body/range -0.0008, upper wick -0.0003, lower wick +0.0006, green streak +0.0007,
red streak +0.0010 — and two are net NEGATIVE, i.e. the model is better without
them. There is no candlestick-pattern effect here. There is a busy-coin effect,
which the tape already measures directly without drawing a candle.

**Finding 4 — none of it predicts price.** Same features, same discipline, target
"price up over the next 5 minutes": baseline 0.523, with chart features 0.525. A
coin flip, exactly as #1 and #9 found through other channels.

CONCLUSION: this is the attention result again, arriving through a third door. We
can see the crowd, we can now also see the crowd REACTING to the chart, and neither
predicts the price. Do not build a candlestick feature set: its measurable content
is trade count and volatility, which are already features. Do not revisit without
a mechanism that is not "activity" in disguise.

---

## #18 — the pre-warn alert cannot beat its own base rate (2026-08-12)

The pre-warn alert fires when p_team_exit10 clears a threshold "chosen for >=93%
precision", and the audit certified it at 94.2%. Both numbers are real and both
are meaningless, because the base rate of "team exits within 10 min" on the
anchor-gated classic population is **96.5%** (n=312).

An alert with 94.2% precision against a 96.5% base rate is 2.3 points WORSE than
assuming every team exits and never alerting at all. The absolute floor
(PREWARN_PRECISION_MIN) could never have detected this: it measures the alert
against a constant instead of against the strategy of not bothering.

It has also fired **0 times** on verified-classic coins, so nothing was lost in
practice — but it would have looked healthy in the audit while being worthless.

Fixed by gating on LIFT (precision - base rate) rather than absolute precision.
The general lesson, which applies to every alert this system will ever add: on a
saturated outcome, precision is a property of the base rate, not of the model.
Always quote the lift.

The live question is not WHETHER the team exits — it is WHEN, and how long the
liquidation runs (median 323s from the first sell). That is a timing problem and
belongs to the v5 hazard model, which is the only place a real signal was found:
rank correlation 0.219 for exit timing against 0.003-0.049 for every price target.

---

## #19 — following the team's path loses money (2026-08-13)

The most direct test yet of the trading thesis: the team's trades are observable,
so copy them. Simulated on 195 anchor-gated coins / 2,810 team buys, with
execution lag swept, limit fills, fees deducted, and no look-ahead.

**Exit when they exit:** median round trip 0.948x, 32.7% of trips win, compounded
median 0.654x per coin. Only 18.0% of coins finish above 1.0.

**Latency is not the problem.** Sweeping the lag: 0s -> 0.956x, 2s -> 0.951x,
5s -> 0.948x, 30s -> 0.960x. Even physically-impossible zero-latency execution
loses. This retires the idea that a faster feed rescues the strategy — it was
tested BEFORE building the free-RPC pipeline, which is the only reason that work
wasn't wasted.

**No exit rule rescues it** once a deadline and 1.5% fees are applied:
  +20% within  60s  mean 0.993      +50% within 300s  mean 0.975
  +20% within 300s  mean 0.973      +50% within 600s  mean 0.954
  +20% within 600s  mean 0.963      +20% unbounded    mean 1.056
Only the UNBOUNDED rule profits, and that is an artifact: "wait indefinitely for
+20%" means holding through a 90% drawdown until the price happens to trade there
again. It is not a trade anyone takes.

**Nor does entry selection**, including the specific rebuy hypothesis:
  first buy (before any team sell)  mean 0.984 (n=155)
  rebuy (after a team sell)         mean 0.961 (n=2616)
  rebuy >=20% below their own sell  mean 0.944 (n=148)
The most targeted version of the thesis is the WORST of the three.

THE TRAP WORTH REMEMBERING: +20%/600s shows a 55.4% win rate and a median of
1.045 while its MEAN is 0.963. You win more often than not and still lose money,
because the losses are larger than the wins. Any future strategy result quoted as
a win rate or a median is uninterpretable — this asset class is defined by its
left tail. Quote the mean, or quote nothing.

This also explains the earlier 1.56x "lift after a team rebuy" (#cycle research):
that used raw MAX price over the following 10 minutes — the best possible exit.
Made executable at a realistic fill it is 0.948x. The gap between those two
numbers is the entire distance between a backtest and a trade.

CONCLUSION: the team's path is DESCRIPTIVE, not tradable. Consistent with #15 and
#17 — the structure is real and measurable, and it does not convert into an entry.
Do not retest without a mechanism that is not "follow the insider".

---

## #20 — the thesis head was predicting its own label's precondition (2026-08-18)

"Team will distribute, ROC 0.937" was the system's headline positive result and
sat at the top of CLAUDE.md's "what works". Audited as adversarially as the
pre-warning alert in #18, it does not survive.

The label is computed as `team_sold_pct = grad_team_pct - current_team_pct`, and
DISTRIBUTING requires that to exceed 30. So a team holding under 30% of supply
CANNOT receive the label — not unlikely, arithmetically impossible. Measured:

    team supply < 30%   n=231   labelled DISTRIBUTE:  0.4%
    team supply >= 30%  n=291   labelled DISTRIBUTE: 70.8%

team_supply_pct is a model FEATURE. Alone it scores ROC 0.944 against the label.
The model's apparent skill is largely reading off whether the label is reachable.

Restricted to the population where the question is genuinely open — teams holding
>=30%, where distribution is possible either way — the model scores **0.618**.
Real, weak, and not the thesis-confirming number it was quoted as for weeks.

THREE PROCESS FAILURES MADE THIS INVISIBLE, and each is now fixed:

1. The single-feature leak canary only tested `survive60` and `moon10x`. The head
   that leaked was never examined. It now tests every head.
2. The threshold was 0.95, above the 0.944 this would have registered. Now 0.90.
3. A comment in the audit excused team_supply_pct at 0.939 as "the thesis itself".
   That is the exact rationalisation a canary exists to overrule, written into the
   canary. Removed.

Known structural couplings are now listed explicitly in KNOWN_LABEL_COUPLINGS
rather than tolerated by a loose threshold — the list records that scrutiny
happened and the head was retired, and anything NOT on it still blocks.

CONSEQUENCE: `distribute` is retired from the blocking bands, which left every
ROC-band head retired and the deployment gate guarding nothing. Rather than
un-retire a contaminated head to restore a gate, the gate moved to the one result
that has survived adversarial scrutiny — the 30s exit alarm, +29.7% lift, 95% CI
[+15.9%, +43.6%], judged on lift over base rate rather than absolute precision.

THE GENERAL LESSON, which is #18's in a new costume: check whether your label is
ATTAINABLE for every row before believing a model predicts it. A feature that
gates label eligibility will look like a brilliant predictor, and the more
mechanical the gate, the better it looks.

---

## #21 — the exit alarm was scored against a past event (2026-08-18)

Reported one day earlier as the system's first established predictive result, and
made its blocking deployment gate: "30s exit alarm, top-20% lift +29.7%, 95% CI
[+15.9%, +43.6%], P(no effect) 0.00%". It was measuring the wrong quantity.

`landmark_row` sets `b_team_exited` when a team sell occurred STRICTLY BEFORE the
checkpoint; `persist_landmark` stores that as `hazard_predictions.team_exited`.
Verified: of rows at checkpoint 30 carrying team_exited=1, **70 of 70** have
`time_to_team_exit_s < 30`. The column records a PAST event. `p_exit` is model_a's
hazard for the NEXT interval. Scoring one against the other measures detection of
something already visible in the covariates — tv_trades, tv_drawdown and
tv_net_flow_recent all reflect the selling that already happened.

Corrected — at-risk rows only, against the interval the model actually predicts:

                        wrong label      correct
        ROC                   0.749        0.568
        top-20% lift        +29.7%        +2.1%
        95% CI    [+15.9%, +43.6%]  [-14.3%, +19.6%]
        P(no effect)          0.00%        41.1%

There is no established predictive result in this system.

WHY NOTHING CAUGHT IT. model_a does not take b_team_exited as a feature, so this
was never a feature-is-label leak and the single-feature canary could not see it:
no individual feature was suspicious, the TARGET was. Every existing guard checks
features against a label and assumes the label means what it is named.

Three things now carry the fix: the label lives in one place (eval/exit_alarm.py)
so the audit gate and the pre-registered pooling test cannot drift apart; the gate
reports rather than blocks, because a permanently failing gate deadlocks the
weekly retrain; and the pre-registration endpoint was corrected before either arm
approached its required n, recorded in the file so the edit is auditable.

THE GENERAL LESSON, and it is the third variant of the same mistake in three days
(#18 precision vs base rate, #20 label attainability, #21 label semantics): verify
what a label MEANS by recomputing it from raw records before believing any metric
built on it. A column named team_exited is not a definition. This is why the
standing rule says never compute a metric and interpret it in the same turn.

---

## #22 — funder reputation does not convert into a trading edge (2026-08-18)

The most promising version of the follow-the-team thesis, and the only one with a
mechanism: same operator, same playbook. It had real supporting structure —

  * exit timing is a persistent FUNDER trait: split-half Spearman +0.437 (p=0.008)
    across 36 repeat funders, stronger than the wallet-level +0.249
  * ranking funders on their EARLY coins predicts their LATER ones out-of-time:
    slow-exit funders peak 2.68x vs 1.48x for fast, difference +1.26x with 95% CI
    [+0.50, +1.92], P(no advantage) 0.0%
  * repeat funders cover 51% of coins, so it is not a niche

Tested with point-in-time reputation (a funder's class for a coin uses only their
strictly earlier coins), the slow/fast boundary fixed on the training era and
applied unchanged, fills/lag/fees inherited from the shared follow_return
machinery, and bootstrapping BY FUNDER:

    all team buys (control)   n=7699  mean 1.022  CI [0.964, 1.102]
    repeat funders only       n=3504  mean 1.027  CI [0.993, 1.046]
    slow-exit funders         n=3036  mean 1.021  CI [0.990, 1.036]
    fast-exit funders         n= 468  mean 1.067  CI [0.971, 1.253]

Conditioning does nothing (1.027 vs 1.022), and the slow/fast split runs BACKWARDS
— fast-exit funders scored nominally higher, on overlapping intervals.

So a +1.26x peak advantage that is real, persistent and out-of-time does NOT
convert into return. That is the sharpest statement yet of this dataset's shape:
structure is measurable and predictable; the money is not.

TWO METHOD NOTES, both of which nearly produced a false positive:

1. I first compared 1.027 against the 0.948x from #19 and it looked like a clear
   edge. Wrong baseline: 0.948x is the exit-when-they-exit rule, while this uses
   +20%/600s. Recomputed like-for-like the control is 1.022 and the edge vanishes.
   Always regenerate the control with the same machinery; never quote a stored
   number from a different rule.

2. The same +20%/600s measurement read 0.963 in #19 and 1.022 here, on a corpus
   that has since grown and now includes anchor-ungated and recovered coins. A
   figure that moves 6 points on a population redefinition is not a constant, and
   any strategy claim built on it inherits that instability.

---

## #23 — the transfer-in gap is real, but only at size (2026-08-18)

A gmgn holder scan flagged a wallet that RECEIVED 20.4M tokens and had already
sold 21.78% of them. Our membership gate requires buyer-and-holder overlap, so a
wallet that never bought cannot be a team member — a whole class of insider we
could not see. Sized and, unusually for this log, partly CONFIRMED.

FIRST MEASUREMENT WAS WRONG, twice over, and both errors flattered the idea:

1. 48.6% of holders had no purchase record and sold at 55.6% vs 48.9% for buyers.
   But buyer capture COLLAPSED to 0.0% for 2026-08-06..09 (the outage window) —
   387 coins with zero buyers recorded. CORRECTION 2026-08-18: I attributed this to
   Helius. Wrong — _reconstruct_bc is passed the Solana Tracker client; the
   parameter is merely NAMED `helius` and the cap comment says "Helius budget",
   both leftovers from a refactor, and I read them as the dependency. The collapse
   was the Solana Tracker outage. BC reconstruction is healthy today (83.9% of
   websocket coins carry buyers). Identifiers renamed so the next reader is not
   misled the same way. Restricted to coins with healthy capture the gap
   is 32.5% of holders and the sell difference is +2.8pp: nothing.
2. "Do they sell" is the wrong question. An insider sells FAST. On timing, holders
   with no purchase record sell LATER than buyers — median 465s vs 219s, difference
   +247s, 95% CI [+194s, +301s], P(earlier) 0.0%. As a class they are the opposite
   of insiders.

THE SIGNAL IS ENTIRELY IN THE SIZE, and averaging destroys it:

    holder class                  n      median first sell
    bought (reference)         6962                   219s
    no purchase, <1%            385                   197s
    no purchase, 1-3%          2867                   605s
    no purchase, 3-10%          201                   817s
    no purchase, >=10%           93                    29s   <- the team median

29s, 95% CI [27s, 29s], P(slower than buyers) 0.0%. A double-digit share of supply
with no purchase record is a gifted position, and it behaves exactly like the team.

LABEL IMPACT: of 139 coins recording NO team exit, 66 (47%) have such a holder that
did sell — matching the 46% blind spot measured for low-supply teams. This is that
blind spot.

IMPLEMENTED as evidence, not as a gate exception. A gate branch alone left the
wallets below the 0.35 score threshold (only 12% of 255 cleared it), and the
overlap component conflates "bought" with "is a top-5 holder" so an `overlap == 0`
test was nearly inert. Carrying full E_overlap is both the honest encoding — the
same fact by a different route — and the one that lets the score reflect it. The
weights are calibrated so this lands exactly at the member threshold, identical to
a plain buyer-and-holder.

Deliberately narrow: ~0.4 wallets per coin, max team size unchanged at 23. The
last time this gate was loosened it produced 86 "team" wallets per coin at 9.8%
insider precision.

APPLIES FORWARD ONLY. 1,547 stored coins keep evidence written before holding_pct
existed. A retroactive backfill was written and DISCARDED: re-evaluating the gate
on stored evidence would have demoted 4,958 rows carrying the legacy
'fallback_top_holder' shape, which the audit explicitly exempts and whose stored
decision the evidence cannot reproduce. A widening change that removes members is
a bug; the correct monotone version promoted 7 rows, which is not worth
re-running the label chain for.

---

## #24 — the wallet graph is real and predicts nothing (2026-08-19)

The pair-level wallet graph was built because coordination.analyze_coin had been
computing a typed edge list on every coin and discarding it. Persisted, it does
show genuine structure:

  * wallet PAIRS recur across coins — 906 of 5,030 funder pairs (18.0%) appear on
    more than one coin, one on 26; same_slot has a pair spanning 56 coins.
    Persistent operator infrastructure is visible at the pair level.
  * selectivity is strongly scale-dependent. Median share of all possible wallet
    pairs each signal links, in the 10-minute window: funder 0.02%, buy_size 0.6%,
    same_slot 1.1%, lockstep_sell 4.6% (to 11.9%). The edge definitions were tuned
    on bonding-curve tapes and two of them degenerate on the post-graduation crowd
    — lockstep_sell at a 2s window is a restatement of "many wallets sold in this
    period", not a coordination signal.

NONE OF THE SHAPE PREDICTS OUTCOME. Graph features over the coordination window
against peak multiple (n=77-78), raw and after removing what early buy count
already explains:

    feature                raw rho      p     partial rho      p
    n_components            +0.105  0.362        +0.267    0.019
    largest_comp_share      +0.051  0.659        -0.201    0.080
    clustered_share         +0.105  0.363        -0.152    0.188
    sel_density             -0.119  0.303        -0.180    0.117
    max_degree_ratio        +0.255  0.025        +0.191    0.097
    funder_edges            +0.387  0.001        +0.353    0.002

funder_edges looked like the exception — it survived the activity control at
p=0.002, clearing Bonferroni for six tests. It is an artifact of our own data
collection:

    funder_edges vs traced-wallet count   rho = +0.890
    traced-wallet count vs peak           rho = +0.385
    funder_edges vs peak, controlling activity AND coverage
                                          rho = +0.098, p = 0.392

A funder edge can only form between two wallets whose funder we happen to have
cached. Coins where more wallets have been traced show more funder edges AND higher
peaks, because both track how much attention the coin has already received. The
feature was measuring the pipeline, not the market.

THE LESSON, which is new: control for your own COLLECTION COVERAGE, not just for
market activity. #17 established that activity confounds structural claims; this
adds that the completeness of your own data does too, and it is easier to miss
because it looks like a feature rather than a bias. Any feature derived from an
enrichment we perform selectively (funding traces, behavioral vectors, holder
snapshots) inherits the selection of that enrichment.

CONCLUSION: the graph is worth keeping as DESCRIPTION — it answers "who moves with
whom" and the pairs recur — and it is not an entry signal. Consistent with #19 and
#22: structure in this market is measurable, persistent, and does not convert.
