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
