"""Ablation — turn each factor / hard-SKIP rule off and measure the delta.

Reveals which hand-tuned rules actually carry signal. Each ablation is a pure
transform on the snapshot feature dict (neutralize the inputs a rule reads),
then the real structural_read runs — so no rules.py change is needed and the
ablation always matches live logic.

Metric per ablation: change in will_distribute PR-AUC (+4h) and in the SKIP
precision for distribution. A rule that carries signal HURTS these when removed.

    uv run python -m eval.ablation [--horizon 4]
"""

from __future__ import annotations

import sys
from copy import deepcopy

import numpy as np

from eval._common import (
    load_samples, replay, distribute_score, average_precision, prf1,
)

# each ablation neutralizes the feature inputs a factor/rule reads
ABLATIONS: dict[str, dict] = {
    "top_holder_concentration": {"top_holder_pct": 0.0, "top3_holder_pct": 0.0},
    "bc_speed":                 {"bc_duration_seconds": 3600},
    "thin_participation":       {"unique_bc_buyers": 999},
    "smart_money":              {"smart_money_count": 0},
    "team_supply":              {"team_supply_pct": 10.0, "team_is_bc_sniper": False},
    "funder_reputation":        {"funder_n": 0, "funder_rug_rate": None, "funder_moon_rate": None},
    "creator_reputation":       {"creator_n": 0, "creator_rug_rate": None},
    "wallet_graph":             {"graph_hits": 0, "graph_rug_hits": 0},
    "pump_ring_velocity":       {"launches_24h": 0, "launches_7d": 0},
    "fingerprint_match":        {"fingerprint_distance": None},
    "launch_slot_snipe":        {"launch_slot_snipe_count": 0},
    "proven_wallets":           {"proven_wallet_count": 0},
    "exit_leader_ring":         {"funder_leader_consistency": None, "funder_choreography_n": None},
}


def _metrics(samples, h):
    dist = [s for s in samples if s.distribute.get(h) is not None]
    scores = np.array([distribute_score(*replay(s.features)) for s in dist])
    y = np.array([1.0 if s.distribute[h] else 0.0 for s in dist])
    pred = np.array([1.0 if replay(s.features)[0] == "SKIP" else 0.0 for s in dist])
    ap = average_precision(scores, y)
    p, _, _ = prf1(pred, y)
    return ap, p, len(dist)


def main() -> None:
    args = sys.argv[1:]
    h = int(args[args.index("--horizon") + 1]) if "--horizon" in args else 4

    samples = load_samples()
    base_ap, base_p, n = _metrics(samples, h)
    print(f"baseline (+{h}h, n={n}): PR-AUC={base_ap:.3f}  SKIP⇒distribute precision={base_p:.3f}\n")
    print(f"{'ablation':<26}{'ΔPR-AUC':>9}{'Δprec':>9}   carries signal?")
    print("─" * 62)

    results = []
    for name, patch in ABLATIONS.items():
        ab = []
        for s in samples:
            s2 = deepcopy(s)
            s2.features = {**s.features, **patch}
            ab.append(s2)
        ap, p, _ = _metrics(ab, h)
        results.append((name, ap - base_ap, p - base_p))

    # sort by absolute PR-AUC impact
    for name, d_ap, d_p in sorted(results, key=lambda r: r[1]):
        verdict = "strong" if d_ap <= -0.01 else "weak/none" if d_ap > -0.003 else "some"
        print(f"{name:<26}{d_ap:>+9.3f}{d_p:>+9.3f}   {verdict}")

    print("\nNegative ΔPR-AUC = removing the rule HURTS ranking ⇒ it carries signal.\n"
          "≈0 = the rule is inert on current data (candidate to drop or re-tune in Phase 3).")


if __name__ == "__main__":
    main()
