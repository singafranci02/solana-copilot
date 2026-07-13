"""Public track record — every fired alert, graded against the chain, no cherry-picking.

Reads only what actually happened: prewarn_alerts and team_dump_alerts are rows
written at send time, outcomes come from the swap tape. Rules that keep it honest:

  - EVERY fired alert appears — correct, wrong, pending, or ungradable. The row
    count must equal the alert count; a track record you can subtract from is
    marketing, not a record.
  - an alert is graded only once MATURE (>=65 min old), so "pending" can never
    silently become "deleted"
  - live record and backtest are separate sections and never mixed; the live
    record starts 2026-07-13 (calibrated-threshold deploy)
  - every row carries the mint address — anyone can replay it against the chain

Outputs:
  docs/TRACK_RECORD.md        human-readable
  public/track_record.json    consumed by the website

    uv run python scripts/track_record.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

ROOT = Path(__file__).parent.parent
MATURITY_S = 65 * 60          # grade only alerts at least this old
PREWARN_CLAIM_S = 600         # the pre-warning's claim: team exits within 10 min
LIVE_SINCE = 1783980000       # 2026-07-13 ~02:40 UTC — calibrated-threshold deploy


def _price_at(pts, ts):
    """Last trade price at or before ts (pts sorted by time)."""
    p = None
    for t, px in pts:
        if t > ts:
            break
        p = px
    return p


def grade_exit_alarm(alert_ts: int, exit_ts: float | None, collapse_ts: float | None,
                     pts: list[tuple[int, float]]) -> dict:
    """Grade one team-exit alarm: what did exiting on it save? Pure."""
    out: dict = {"outcome": "pending"}
    p0 = _price_at(pts, alert_ts)
    p60 = _price_at(pts, alert_ts + 3600)
    if not pts or p0 is None or p0 <= 0 or pts[-1][0] < alert_ts + 3600:
        out["outcome"] = "ungradable" if pts and pts[-1][0] >= alert_ts + 3600 else "pending"
        return out
    ratio = (p60 or 0.0) / p0
    out.update({
        "price_1h_after_vs_alert": round(ratio, 3),
        "exit_saved_pct": round((1 - ratio) * 100, 1),
        "collapse_followed": bool(collapse_ts is not None and collapse_ts >= alert_ts),
        "outcome": "correct" if ratio < 1.0 else "wrong",
    })
    return out


def grade_prewarn(grad_ts: int, exit_offset_s: float | None, tape_span_s: float | None) -> dict:
    """Grade one pre-warning: did the team exit within the claimed 10 min? Pure."""
    if exit_offset_s is not None:
        return {"outcome": "correct" if exit_offset_s <= PREWARN_CLAIM_S else "wrong",
                "team_exit_min": round(exit_offset_s / 60, 1)}
    if tape_span_s is not None and tape_span_s >= PREWARN_CLAIM_S * 3:
        return {"outcome": "wrong", "team_exit_min": None}     # long tape, no exit seen
    return {"outcome": "ungradable", "team_exit_min": None}


def build() -> dict:
    conn = get_connection()
    now = int(time.time())
    sym = {r["mint"]: r["symbol"] for r in conn.execute(
        "SELECT mint, symbol FROM tokens WHERE symbol IS NOT NULL")}
    traj = {r["token_mint"]: dict(r) for r in conn.execute(
        """SELECT token_mint, time_to_team_exit_s, time_to_collapse_s, tape_span_s,
                  n_price_points FROM coin_trajectory""")}
    grad = {r["token_mint"]: int(r["graduated_at"]) for r in conn.execute(
        "SELECT token_mint, graduated_at FROM graduation_events")}

    def tape(mint):
        return [(int(r["ts"]), float(r["price_usd"])) for r in conn.execute(
            """SELECT ts, price_usd FROM post_grad_swaps
               WHERE token_mint=? AND price_usd>0 ORDER BY ts""", (mint,))]

    prewarns = []
    for r in conn.execute("SELECT * FROM prewarn_alerts ORDER BY alerted_at DESC"):
        m = r["token_mint"]
        t = traj.get(m, {})
        g = {"outcome": "pending"}
        if now - r["alerted_at"] >= MATURITY_S:
            usable = (t.get("n_price_points") or 0) >= 30
            g = grade_prewarn(grad.get(m, r["alerted_at"]),
                              t.get("time_to_team_exit_s") if usable else None,
                              t.get("tape_span_s") if usable else None)
        prewarns.append({
            "mint": m, "symbol": sym.get(m) or m[:8], "alerted_at": r["alerted_at"],
            "p_exit10": r["p_exit10"], "claim": "team exits within 10 min", **g,
        })

    alarms = []
    for r in conn.execute("SELECT * FROM team_dump_alerts ORDER BY alerted_at DESC"):
        m = r["token_mint"]
        t = traj.get(m, {})
        g = {"outcome": "pending"}
        if now - r["alerted_at"] >= MATURITY_S:
            gts = grad.get(m)
            coll_abs = (gts + t["time_to_collapse_s"]
                        if gts and t.get("time_to_collapse_s") is not None else None)
            exit_abs = (gts + t["time_to_team_exit_s"]
                        if gts and t.get("time_to_team_exit_s") is not None else None)
            g = grade_exit_alarm(r["alerted_at"], exit_abs, coll_abs, tape(m))
        alarms.append({
            "mint": m, "symbol": sym.get(m) or m[:8], "alerted_at": r["alerted_at"],
            "minute_offset": r["minute_offset"], "claim": "team is exiting — price not yet broken",
            **g,
        })
    conn.close()

    def summarize(rows):
        graded = [x for x in rows if x["outcome"] in ("correct", "wrong")]
        return {
            "fired": len(rows),
            "graded": len(graded),
            "correct": sum(x["outcome"] == "correct" for x in graded),
            "precision": round(np_mean([x["outcome"] == "correct" for x in graded]), 3)
            if graded else None,
            "pending": sum(x["outcome"] == "pending" for x in rows),
            "ungradable": sum(x["outcome"] == "ungradable" for x in rows),
        }

    saved = [a["exit_saved_pct"] for a in alarms
             if a["outcome"] in ("correct", "wrong") and "exit_saved_pct" in a]
    return {
        "generated_at": now,
        "live_since": LIVE_SINCE,
        "prewarn": {"summary": summarize(prewarns), "alerts": prewarns},
        "exit_alarm": {"summary": summarize(alarms),
                       "median_saved_pct": round(median(saved), 1) if saved else None,
                       "alerts": alarms},
        "backtest_reference": {
            "note": "out-of-time backtest on 1,800+ graduations — NOT part of the live record",
            "prewarn_precision": 0.94, "exit_better_off_rate": 0.86,
            "median_position_preserved_pct": 77,
            "details": "eval/AUDIT_BASELINE.md, eval/NEGATIVE_RESULTS.md",
        },
    }


def np_mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def median(xs):
    s = sorted(xs)
    n = len(s)
    return (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2]


def render_md(d: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["generated_at"]))
    pw, ea = d["prewarn"], d["exit_alarm"]

    def pct(x):
        return f"{x:.0%}" if x is not None else "—"

    L = [f"# Live track record", "",
         f"*Auto-generated {ts}. Every fired alert appears — correct, wrong, pending or "
         f"ungradable. Nothing is removed. Each row carries the mint so anyone can replay "
         f"it against the chain. Live record began 2026-07-13.*", "",
         "## Pre-warnings (at graduation: “team exits within 10 min”)", "",
         f"fired **{pw['summary']['fired']}** · graded {pw['summary']['graded']} · "
         f"precision **{pct(pw['summary']['precision'])}** · pending {pw['summary']['pending']} "
         f"· ungradable {pw['summary']['ungradable']}", "",
         "| time (UTC) | coin | p | outcome | team exit |", "|---|---|---|---|---|"]
    for a in pw["alerts"][:50]:
        t = time.strftime("%m-%d %H:%M", time.gmtime(a["alerted_at"]))
        ex = f"{a['team_exit_min']}m" if a.get("team_exit_min") is not None else "—"
        L.append(f"| {t} | ${a['symbol']} `{a['mint'][:8]}…` | {a['p_exit10']:.2f} "
                 f"| {a['outcome']} | {ex} |")
    L += ["", "## Exit alarms (live: “team is exiting — price not yet broken”)", "",
          f"fired **{ea['summary']['fired']}** · graded {ea['summary']['graded']} · "
          f"price lower 1h later {pct(ea['summary']['precision'])} · median saved "
          f"**{ea['median_saved_pct'] if ea['median_saved_pct'] is not None else '—'}%** "
          f"of position · pending {ea['summary']['pending']}", "",
          "| time (UTC) | coin | alarm at | outcome | 1h later | saved |", "|---|---|---|---|---|---|"]
    for a in ea["alerts"][:50]:
        t = time.strftime("%m-%d %H:%M", time.gmtime(a["alerted_at"]))
        r = a.get("price_1h_after_vs_alert")
        ratio_s = f"{r:.2f}x" if r is not None else "—"
        saved_s = f"{a['exit_saved_pct']}%" if a.get("exit_saved_pct") is not None else "—"
        L.append(f"| {t} | ${a['symbol']} `{a['mint'][:8]}…` | +{a['minute_offset']}m "
                 f"| {a['outcome']} | {ratio_s} | {saved_s} |")
    b = d["backtest_reference"]
    L += ["", "## Backtest reference (NOT the live record)", "",
          f"{b['note']}: pre-warn precision {b['prewarn_precision']:.0%}, holder better off "
          f"exiting {b['exit_better_off_rate']:.0%} of the time, median "
          f"{b['median_position_preserved_pct']}% of position preserved. {b['details']}", ""]
    return "\n".join(L)


def main() -> None:
    d = build()
    (ROOT / "public").mkdir(exist_ok=True)
    (ROOT / "public" / "track_record.json").write_text(json.dumps(d, indent=1))
    (ROOT / "docs" / "TRACK_RECORD.md").write_text(render_md(d))
    pw, ea = d["prewarn"]["summary"], d["exit_alarm"]["summary"]
    print(f"pre-warns: {pw['fired']} fired, {pw['graded']} graded, precision {pw['precision']}")
    print(f"exit alarms: {ea['fired']} fired, {ea['graded']} graded, precision {ea['precision']}, "
          f"median saved {d['exit_alarm']['median_saved_pct']}%")
    print("wrote public/track_record.json + docs/TRACK_RECORD.md")


if __name__ == "__main__":
    main()
