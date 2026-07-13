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
