"""Three-portfolio economic comparison — the number that actually matters.

Paper only. No execution anywhere (analysis-only codebase). Compares:
  A. rule portfolio   — hold every SOUND, avoid every SKIP
  B. buy-all baseline — every graduation
  C. random control   — a random 1/N sample of graduations (seeded)

For each, the outcome distribution (moon/ok/rug) and the median MC multiple at
1h/4h/24h. The rule portfolio must beat buy-all on RUG-AVOIDANCE to justify the
pipeline — winner-picking is explicitly not the claim.

    uv run python -m eval.economic_backtest [--seed 0]
"""

from __future__ import annotations

import sys

import numpy as np

from eval._common import HORIZONS, load_samples, replay, day_bucket


def _multiple(pct):
    return (1.0 + pct / 100.0) if pct is not None else None


def _summarize(name: str, samples, h: int) -> None:
    labeled = [s for s in samples if s.outcome.get(h) is not None]
    if not labeled:
        print(f"  {name:<16} +{h}h  no labeled outcomes yet")
        return
    n = len(labeled)
    rug = sum(1 for s in labeled if s.outcome[h] == "rug") / n
    ok = sum(1 for s in labeled if s.outcome[h] == "ok") / n
    moon = sum(1 for s in labeled if s.outcome[h] == "moon") / n
    mults = [m for m in (_multiple(s.mc_change_pct[h]) for s in labeled) if m is not None]
    med = float(np.median(mults)) if mults else float("nan")
    print(f"  {name:<16} +{h}h  n={n:<4} rug={rug:5.1%} ok={ok:5.1%} "
          f"moon={moon:5.1%}  median_mult={med:.2f}×")


def main() -> None:
    args = sys.argv[1:]
    seed = int(args[args.index("--seed") + 1]) if "--seed" in args else 0
    rng = np.random.default_rng(seed)

    samples = load_samples()
    verdicts = {s.token_mint: replay(s.features)[0] for s in samples}

    rule = [s for s in samples if verdicts[s.token_mint] == "STRUCTURALLY_SOUND"]
    buy_all = samples
    k = max(len(rule), 1)
    idx = rng.choice(len(samples), size=min(k * 4, len(samples)), replace=False)
    random_ctrl = [samples[i] for i in idx]

    print(f"span {day_bucket(samples[0].graduated_at)} → {day_bucket(samples[-1].graduated_at)}  "
          f"· {len(samples)} graduations")
    print(f"rule portfolio (SOUND): {len(rule)}  ·  buy-all: {len(buy_all)}  ·  "
          f"random control: {len(random_ctrl)}\n")

    for h in HORIZONS:
        print(f"── +{h}h ─────────────────────────────────────────────")
        _summarize("A · rule SOUND", rule, h)
        _summarize("B · buy-all", buy_all, h)
        _summarize("C · random", random_ctrl, h)
        print()

    # headline: rug-avoidance edge at 24h (or 4h if 24h thin)
    for h in (24, 4):
        r = [s for s in rule if s.outcome.get(h) is not None]
        b = [s for s in buy_all if s.outcome.get(h) is not None]
        if len(r) >= 5 and b:
            rr = sum(1 for s in r if s.outcome[h] == "rug") / len(r)
            br = sum(1 for s in b if s.outcome[h] == "rug") / len(b)
            edge = br - rr
            print(f"RUG-AVOIDANCE EDGE +{h}h: rule {rr:.1%} vs buy-all {br:.1%} "
                  f"→ {edge:+.1%} ({'BEATS' if edge > 0 else 'does NOT beat'} baseline) "
                  f"[rule n={len(r)}]")
            break
    else:
        print("RUG-AVOIDANCE EDGE: too few labeled SOUND outcomes yet — re-run as data accrues.")


if __name__ == "__main__":
    main()
