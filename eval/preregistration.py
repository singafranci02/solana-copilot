"""PRE-REGISTERED decision rule: may classic and Mayhem be pooled for training?

Registered 2026-08-18, BEFORE the Mayhem data existed in useful quantity
(156 samples at time of writing, against a required 1,600 per arm). Nothing in
this file may be changed after looking at the answer. tests/test_preregistration.py
pins every constant, so editing one is a visible act in the diff rather than a
quiet adjustment.

WHY PRE-REGISTER THIS ONE

Pooling is worth roughly 10x the training data, and every model head in this
system is currently starved below the 500-row minimum. That is an enormous
incentive to read an ambiguous result as permission. The failure mode is not
dishonesty — it is that "no significant difference" is what small samples always
say, and it is trivially reframed as "they look the same".

So the burden is inverted. This is an EQUIVALENCE test, not a difference test:
pooling requires POSITIVE evidence that the two populations behave alike. Failing
to detect a difference is not evidence of similarity and does not license pooling.

THE PRIMARY ENDPOINT — deliberately the thing we would actually rely on

Not a descriptive statistic. The question is whether a model trained on classic
TRANSFERS to Mayhem, so the endpoint is the 30s exit alarm's top-quintile lift:

    train on CLASSIC only -> score MAYHEM -> measure lift
    equivalent iff |lift_mayhem - lift_classic| <= EQUIVALENCE_MARGIN
    AND both lifts individually exceed MIN_INDIVIDUAL_LIFT

Descriptive comparisons (collapse rate, median exit) are reported but are NEVER
decisive. They were what first suggested the populations differ, and then agreed
once time-matched — which is exactly why they are too fragile to decide on.

THE SIZE REQUIREMENT, computed before the rule was written

Bootstrapped SD of the lift at the observed effect size, doubled for a difference
of two arms, at 95%:

        n/arm      SD(lift)    SD(diff)    95% half-width
          176         0.076       0.108          21.1%
          500         0.055       0.078          15.2%
         1200         0.042       0.059          11.6%

Half-width scales as 1/sqrt(n), so clearing a 10pp margin needs ~1,600 per arm.
That is the number registered below. At the classic accrual rate (~22 usable
observations/day) this is roughly ten weeks — genuinely expensive, and registered
anyway. Widening the margin to something reachable is the precise move this file
exists to prevent.

    uv run python -m eval.preregistration
"""

from __future__ import annotations

import sys

import numpy as np

# ── REGISTERED CONSTANTS — frozen 2026-08-18, pinned by tests ────────────────────
CHECKPOINT_S = 30              # the only checkpoint with a balanced base rate
ALARM_QUANTILE = 0.80          # top 20%, the registered operating point
EQUIVALENCE_MARGIN = 0.10      # 10pp on the lift difference
MIN_INDIVIDUAL_LIFT = 0.10     # each arm must beat its own base rate by this
N_REQUIRED_PER_ARM = 1600      # from the sizing table above
BOOTSTRAP_DRAWS = 3000
REGISTERED_ON = "2026-08-18"


def _lift(p: np.ndarray, y: np.ndarray) -> float | None:
    if len(y) < 20 or not 0 < y.mean() < 1:
        return None
    m = p >= np.quantile(p, ALARM_QUANTILE)
    if m.sum() < 8:
        return None
    return float(y[m].mean() - y.mean())


def _load(conn, platform: str):
    """SPECIFICATION CORRECTION, 2026-08-18, before any data reached the required n.

    As first written this read hazard_predictions.team_exited, which records whether
    the team had ALREADY sold before the checkpoint rather than whether it sells in
    the interval being scored (NEGATIVE_RESULTS #21). The pre-registered endpoint
    would have compared two populations on the wrong quantity.

    Correcting a specification error before the data arrives is not p-hacking — the
    test has never been run to a verdict, both arms remain far below N_REQUIRED
    (176 and 615 against 1,600), and the change makes the endpoint stricter rather
    than easier. Recording it here so the edit is auditable rather than silent.
    """
    from eval.exit_alarm import at_risk_rows
    return at_risk_rows(conn, platform, CHECKPOINT_S)


def evaluate(conn) -> dict:
    pc, yc = _load(conn, "pump.fun")
    pm, ym = _load(conn, "mayhem")
    res = {"n_classic": len(yc), "n_mayhem": len(ym),
           "n_required": N_REQUIRED_PER_ARM, "verdict": None}

    # REFUSAL. Below the registered n this script does not produce a verdict, and
    # does not produce a "leaning" either — a directional hint is how an
    # underpowered test becomes a decision.
    if len(yc) < N_REQUIRED_PER_ARM or len(ym) < N_REQUIRED_PER_ARM:
        res["verdict"] = "INSUFFICIENT_DATA"
        return res

    lc, lm = _lift(pc, yc), _lift(pm, ym)
    if lc is None or lm is None:
        res["verdict"] = "INSUFFICIENT_DATA"
        return res

    rng = np.random.default_rng(0)
    diffs = []
    for _ in range(BOOTSTRAP_DRAWS):
        a = rng.integers(0, len(yc), len(yc))
        b = rng.integers(0, len(ym), len(ym))
        la, lb = _lift(pc[a], yc[a]), _lift(pm[b], ym[b])
        if la is not None and lb is not None:
            diffs.append(lb - la)
    d = np.array(diffs)
    lo, hi = np.percentile(d, [2.5, 97.5])

    equivalent = (abs(lo) <= EQUIVALENCE_MARGIN and abs(hi) <= EQUIVALENCE_MARGIN
                  and lc >= MIN_INDIVIDUAL_LIFT and lm >= MIN_INDIVIDUAL_LIFT)
    res.update(lift_classic=lc, lift_mayhem=lm, diff_lo=float(lo), diff_hi=float(hi),
               verdict="POOL" if equivalent else "KEEP_SEPARATE")
    return res


def main() -> int:
    from src.common.db import get_connection

    conn = get_connection()
    r = evaluate(conn)
    conn.close()

    print(f"PRE-REGISTERED POOLING TEST   (registered {REGISTERED_ON})")
    print(f"  margin ±{EQUIVALENCE_MARGIN:.0%} on lift difference, "
          f"{N_REQUIRED_PER_ARM} observations required per arm\n")
    print(f"  classic : {r['n_classic']:5} / {r['n_required']}")
    print(f"  mayhem  : {r['n_mayhem']:5} / {r['n_required']}")

    if r["verdict"] == "INSUFFICIENT_DATA":
        print("\n  VERDICT: none. Below the registered sample size.")
        print("  This is not 'they look similar' and not 'lean toward pooling'.")
        print("  It is the absence of a result, and it licenses nothing.")
        return 2

    print(f"\n  lift classic {r['lift_classic']:+.1%} | mayhem {r['lift_mayhem']:+.1%}")
    print(f"  difference 95% CI [{r['diff_lo']:+.1%}, {r['diff_hi']:+.1%}]")
    print(f"\n  VERDICT: {r['verdict']}")
    return 0 if r["verdict"] == "POOL" else 1


if __name__ == "__main__":
    sys.exit(main())
