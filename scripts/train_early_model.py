"""Train the T+5min EARLY ATTENTION model — the head that reads the pump.

Separate artifact from verdict_model_v4 on purpose: this model is scored at a
DIFFERENT MOMENT (5 minutes after graduation, once the crowd is visible) and off a
DIFFERENT feature set (order flow, not structure). Mixing them would leak the early
window into the graduation-time verdict.

What it predicts (expanding walk-forward, out-of-time):
    survive>=60min, coins still alive at T+5m   ROC 0.904   top-5% survive 100% (2.7x)
    survive>=60min, structure @T+0 (reference)  ROC 0.806

What it does NOT predict — the 10x. A first pass appeared to hit ROC 0.731, but the
audit showed that was `price_run` LEAKING THE LABEL: 36% of coins that reached 10x
did so INSIDE the 5-minute window, so the feature contained the answer. Correcting
for it:
    reached_10x, all features                     ROC 0.746  (leaky, worthless)
    reached_10x, price_run removed                ROC 0.623
    reached_10x, only 10x still FUTURE at T+5m    ROC 0.592
    reached_10x, both corrections                 ROC 0.517  <- a coin flip

Structure gives 0.583 on the same target. So the pump is unpredictable from BOTH
graduation structure AND early order flow. The moon10x head is therefore NOT trained
here: a "pump detector" that only fires once the pump is already visible in the price
is detection, not discrimination, and has no value.

Rebuilds the training rows from the stored post_grad_swaps tape, so it trains on
exactly what the live path computes at 5 minutes — same function, same window.

    uv run python scripts/train_early_model.py
"""

import pickle
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from eval.model import platt_fit, roc_auc
from src.analyzer.early_attention import (
    DEFAULT_WINDOW_S, compute_early_attention, to_features,
)
from src.common.db import get_connection

MODEL_VERSION = "early-v1-attention-gbm-platt"
OUT = Path(__file__).parent.parent / "models" / "early_model_v1.pkl"
TARGETS = ("survive60",)   # moon10x REMOVED — see the negative result below


def _prep(X, med):
    M = np.isnan(X).astype(float)
    return np.hstack([np.where(np.isnan(X), med, X), M])


def _fit_gbm(A, y):
    from sklearn.ensemble import GradientBoostingClassifier
    m = GradientBoostingClassifier(
        n_estimators=300, learning_rate=0.05, max_depth=3,
        subsample=0.8, min_samples_leaf=30, random_state=0)
    m.fit(A, y)
    return m


def load_rows(window_s: int = DEFAULT_WINDOW_S):
    """Rebuild (features, labels) from the tape, via the LIVE feature function."""
    conn = get_connection()
    teams = {r["token_mint"]: set((r["member_addresses"] or "").split(","))
             for r in conn.execute(
                 "SELECT token_mint, REPLACE(REPLACE(REPLACE(member_addresses,'[',''),']',''),'\"','') "
                 "AS member_addresses FROM team_clusters")}
    traj = {r["token_mint"]: dict(r) for r in conn.execute(
        "SELECT token_mint, reached_10x, time_to_collapse_s, tape_span_s FROM coin_trajectory")}

    swaps: dict[str, list] = {}
    for r in conn.execute(
        """SELECT pgs.token_mint, pgs.ts, pgs.side, pgs.wallet_address, pgs.sol_amount,
                  pgs.price_usd, ge.graduated_at
           FROM post_grad_swaps pgs JOIN graduation_events ge USING (token_mint)
           WHERE pgs.ts <= ge.graduated_at + ?
           ORDER BY pgs.ts""", (window_s,)):
        swaps.setdefault(r["token_mint"], []).append(
            (SimpleNamespace(timestamp=int(r["ts"]), side=r["side"],
                             signer=r["wallet_address"],
                             sol_amount=r["sol_amount"], price_usd=r["price_usd"]),
             int(r["graduated_at"])))

    grad_at = {r["token_mint"]: int(r["graduated_at"])
               for r in conn.execute("SELECT token_mint, graduated_at FROM graduation_events")}
    conn.close()

    rows = []
    for mint, pairs in swaps.items():
        t = traj.get(mint)
        if not t or mint not in grad_at:
            continue
        a = compute_early_attention([s for s, _ in pairs], grad_at[mint],
                                    teams.get(mint, set()), window_s)
        if a is None:
            continue
        rows.append((grad_at[mint], to_features(a), t))
    rows.sort(key=lambda r: r[0])       # TIME order — walk-forward depends on it
    return rows


def _label(t: dict, target: str):
    if target == "moon10x":
        return float(t["reached_10x"]) if t["reached_10x"] is not None else None
    if target == "survive60":
        span, coll = t.get("tape_span_s") or 0, t.get("time_to_collapse_s")
        if coll is not None:
            return float(coll >= 3600)
        return 1.0 if span >= 3600 else None      # censored before 60min → unusable
    return None


def train_head(rows, target: str) -> dict | None:
    rr = [(f, _label(t, target)) for _, f, t in rows]
    rr = [(f, y) for f, y in rr if y is not None]
    if len(rr) < 200:
        print(f"  {target:<11} SKIPPED (only {len(rr)} labeled rows)")
        return None
    keys = sorted(rr[0][0].keys())
    X = np.array([[f.get(k, np.nan) for k in keys] for f, _ in rr], dtype=float)
    y = np.array([y for _, y in rr])

    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)

    icut = int(len(X) * 0.8)            # held-back TIME tail — never the future
    inner = _fit_gbm(_prep(X[:icut], med), y[:icut])
    p_tail = inner.predict_proba(_prep(X[icut:], med))[:, 1]
    platt = platt_fit(p_tail, y[icut:])
    tail_roc = roc_auc(p_tail, y[icut:])

    k = max(int(len(p_tail) * 0.05), 1)
    hit = y[icut:][np.argsort(-p_tail)[:k]].mean()
    base = y.mean()
    print(f"  {target:<11} n={len(y):<5} base={base:5.1%}  tail ROC={tail_roc:.3f}  "
          f"top5%={hit:5.1%} ({hit/max(base,1e-9):.1f}x lift)")

    return {"keys": keys, "median": med, "sk_model": _fit_gbm(_prep(X, med), y),
            "platt": platt, "n": int(len(y)), "base_rate": float(base),
            "tail_roc": float(tail_roc), "top5_hit": float(hit)}


def main() -> None:
    rows = load_rows()
    print(f"training on {len(rows)} coins with a readable T+{DEFAULT_WINDOW_S}s tape")
    heads = {t: h for t in TARGETS if (h := train_head(rows, t))}
    if not heads:
        print("no heads trained — aborting"); return

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "wb") as fh:
        pickle.dump({"version": MODEL_VERSION, "trained_at": int(time.time()),
                     "window_s": DEFAULT_WINDOW_S, "heads": heads,
                     "note": "scored at T+5min from order flow — NOT a graduation-time verdict"}, fh)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)  version={MODEL_VERSION}")


if __name__ == "__main__":
    main()
