"""Do poll-recovered coins behave like WebSocket-detected ones?

The WebSocket misses ~27% of graduations; scripts/graduation_backstop.py recovers
them. Both populations enter training with identical labels and an identical code
path, so if recovery selects a BIASED subpopulation it biases every model — and
nothing else in the system would notice, because a recovered coin is
indistinguishable from a live one once stored.

There are concrete reasons to expect divergence, which is why this is measured
rather than assumed:

  * a coin the WebSocket missed may be missed for a reason correlated with outcome
    (thin early activity, unusual pool setup, a burst the feed dropped under load)
  * recovery only succeeds when the tape can be walked back to the anchor, which
    favours QUIETER coins — busier ones exceed the page budget and are skipped.
    That is a selection effect pointing in a specific direction: poll-recovered
    coins should trade less, and if trading volume correlates with collapse, the
    populations will differ on outcome too.

Reported as a FINDING, and blocking only when a difference is both established
(bootstrap CI excludes zero) and large enough to matter. A permanently failing
gate deadlocks the weekly retrain, which is the failure that stranded the models
on corrupted labels for six days.

    uv run python -m eval.source_parity
"""

from __future__ import annotations

import sys

import numpy as np

MIN_PER_ARM = 60          # below this the CI is too wide to conclude anything
MATURITY_S = 14400        # 4h. Collapse needs time to happen, and poll-recovered
                          # coins are all recent, so an unmatched comparison reads
                          # their youth as survival. First run without this showed
                          # a 94.2% vs 40.0% collapse gap that is mostly age.
MATERIAL_DIFF = 0.20      # 20pp — a difference this large changes what training sees
BOOTSTRAP_DRAWS = 2000

# Anchor-gated classic population, split by how the coin reached us. The gate must
# match eval._common so this compares the coins the trainer actually uses.
_POP = """
    SELECT ge.detection_source AS src,
           ct.collapsed        AS collapsed,
           CASE WHEN ct.time_to_team_exit_s IS NOT NULL THEN 1 ELSE 0 END AS exited,
           ct.n_price_points   AS prints
    FROM coin_trajectory ct
    JOIN graduation_events ge USING(token_mint)
    JOIN tokens t ON t.mint = ct.token_mint
    WHERE t.platform = 'pump.fun' AND COALESCE(ge.is_manufactured,0) = 0
      AND ct.n_price_points >= 30
      AND (SELECT MIN(p.ts) FROM post_grad_swaps p
           WHERE p.token_mint = ct.token_mint)
          BETWEEN ge.graduated_at AND ge.graduated_at + 120
      AND ge.graduated_at < strftime('%s','now') - :maturity
"""

# team-exit is EXPECTED to differ and is reported, never a finding: a recovered
# coin has no team cluster (the cluster is a graduation-moment holder read), so its
# exit label is structurally NULL. Flagging that as divergence would be flagging a
# definition.
METRICS = (("collapse rate", "collapsed", True),
           ("team-exit observed", "exited", False),
           ("median price prints", "prints", True))


def compare(conn) -> list[dict]:
    rows = conn.execute(_POP, {"maturity": MATURITY_S}).fetchall()
    ws = [r for r in rows if r["src"] == "websocket"]
    pl = [r for r in rows if r["src"] == "poll"]
    out = []
    rng = np.random.default_rng(0)

    for label, col, is_finding in METRICS:
        a = np.array([r[col] for r in ws], dtype=float)
        b = np.array([r[col] for r in pl], dtype=float)
        stat = np.median if col == "prints" else np.mean
        rec = {"metric": label, "n_ws": len(a), "n_poll": len(b),
               "ws": float(stat(a)) if len(a) else float("nan"),
               "poll": float(stat(b)) if len(b) else float("nan")}
        if len(a) < MIN_PER_ARM or len(b) < MIN_PER_ARM:
            rec["verdict"] = "SUSPENDED"
            out.append(rec)
            continue
        diffs = [stat(b[rng.integers(0, len(b), len(b))])
                 - stat(a[rng.integers(0, len(a), len(a))])
                 for _ in range(BOOTSTRAP_DRAWS)]
        lo, hi = np.percentile(diffs, [2.5, 97.5])
        rec["lo"], rec["hi"] = float(lo), float(hi)
        established = lo > 0 or hi < 0
        # 'prints' is a count, so the material threshold is relative not absolute
        scale = max(abs(rec["ws"]), 1e-9) if col == "prints" else 1.0
        material = abs(rec["poll"] - rec["ws"]) / scale >= MATERIAL_DIFF
        if not is_finding:
            rec["verdict"] = ("expected (no team cluster on recovery)"
                              if established else "no difference detected")
        else:
            rec["verdict"] = ("DIVERGENT" if established and material
                              else "established but small" if established
                              else "no difference detected")
        out.append(rec)
    return out


def has_material_divergence(results: list[dict]) -> bool:
    return any(r.get("verdict") == "DIVERGENT" for r in results)


def main() -> int:
    from src.common.db import get_connection

    conn = get_connection()
    res = compare(conn)
    conn.close()

    print("DETECTION-SOURCE PARITY — websocket vs poll-recovered (classic, gated)\n")
    print(f"{'metric':>22} {'websocket':>11} {'poll':>9} {'95% CI of diff':>22}  verdict")
    print("-" * 88)
    for r in res:
        ci = (f"[{r['lo']:+.3f}, {r['hi']:+.3f}]" if "lo" in r else "—")
        print(f"{r['metric']:>22} {r['ws']:>11.3f} {r['poll']:>9.3f} {ci:>22}  "
              f"{r['verdict']}  (n {r['n_ws']}/{r['n_poll']})")

    if any(r["verdict"] == "SUSPENDED" for r in res):
        print(f"\nSUSPENDED metrics have under {MIN_PER_ARM} coins in an arm. That is "
              "not 'the populations agree' — it is no result.")
    return 1 if has_material_divergence(res) else 0


if __name__ == "__main__":
    sys.exit(main())
