"""Full-pipeline audit — verifies every stage from raw tape to delivered alert.

Every bug this project has hit was a silent corruption that made numbers look BETTER
or kept looking plausible: fake 26% moon rate (single bad prints), fake 53% survival
(thin tapes), a 0.746 "pump predictor" (price_run leaking the label), 86-wallet
"teams" (additive evidence). This audit exists so none of them can happen again
without a red FAIL. Stages:

  1. DATA      — tape/snapshot/membership integrity (gate enforcement included)
  2. LABELS    — trajectories recompute identically; base rates inside measured bands
  3. LEAKS     — replay fidelity; forbidden heads/features; single-feature ROC canary
  4. BACKTEST  — walk-forward ROC per head vs baseline bands; a head that suddenly
                 IMPROVES past its band FAILS too (a jump on moon10x means a leak,
                 not a breakthrough — that head is measured-unpredictable)
  5. ALERTS    — pre-warning precision/fire-rate and exit-alarm value, simulated
                 out-of-time exactly as they would have fired live
  6. CALIBRATION — calibrated p_rug must beat the base-rate Brier and track realized
                 frequencies per decile

    uv run python -m eval.audit            # full (few minutes: trains walk-forward)
    uv run python -m eval.audit --quick    # stages 1-3 only (seconds; pre-deploy)

Exit code 0 = all pass. Non-zero = something regressed; do not deploy on top of it.
Each run is appended to backtest_runs for trend tracking.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

import numpy as np

from eval._common import load_samples, replay, brier, calibration_bins
from eval.model import (
    TRAINERS, feature_names, build_matrix_nan, roc_auc, platt_fit, platt_apply,
    _label, _trajectory,
)
from src.common.db import get_connection

# ── measured baseline bands (source: eval/BASELINE.md, MODEL_BASELINE.md,
#    NEGATIVE_RESULTS.md; every number was measured out-of-time on this tape) ──────

ROC_BANDS = {
    # head: (min ok, max plausible). Above max = suspicious jump -> audit for a leak.
    "distribute":  (0.88, 0.97),   # measured 0.937
    "rug":         (0.84, 0.96),   # measured 0.912
    "survive60":   (0.70, 0.90),   # measured ~0.81
    "team_exit10": (0.66, 0.86),   # measured 0.765 on gated labels
    "moon10x":     (None, 0.68),   # measured 0.583 == UNPREDICTABLE; "working" = leak
}
BASE_RATE_BANDS = {
    "survive60":   (0.06, 0.30),   # 15.7%; the fake-survivor bug read 53%
    "moon10x":     (0.03, 0.16),   # 9-10%; the bad-print bug read 26%
    "team_exit10": (0.40, 0.80),   # 62-66%
    "rug":         (0.75, 0.97),   # 89%
}
REPLAY_FIDELITY_MIN = 0.995        # rules replayed from snapshots vs stored verdicts
SINGLE_FEATURE_ROC_MAX = 0.95      # any lone snapshot feature this good = leaked label
PREWARN_THRESHOLD = 0.90
PREWARN_PRECISION_MIN = 0.85       # measured 94.2%
PREWARN_FIRE_BAND = (0.03, 0.40)   # era-calibrated fire rate drifts (6-23%) while
                                   # precision holds; the floor only catches "alert died"
EXIT_MEDIAN_MAX = 0.50             # median 1h-after-exit multiple; measured 0.24x
EXIT_BETTER_OFF_MIN = 0.75         # measured 86%
TRAJ_RECOMPUTE_TOLERANCE = 0.02    # <=2% of mature coins may legitimately differ


@dataclass
class Check:
    stage: str
    name: str
    ok: bool
    detail: str


def _walk_forward(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Expanding out-of-time folds — the only split this codebase trusts."""
    n = len(y)
    edges = np.linspace(int(n * 0.4), n, 6).astype(int)
    preds, labels = [], []
    for i in range(5):
        a, b = edges[i], edges[i + 1]
        if b - a < 25 or a < 150 or y[:a].sum() < 8 or (1 - y[:a]).sum() < 8:
            continue
        preds.append(TRAINERS["gbm"](X[:a], y[:a])(X[a:b]))
        labels.append(y[a:b])
    return np.concatenate(preds), np.concatenate(labels)


# ── stage 1: data integrity ────────────────────────────────────────────────────────

def stage_data(conn) -> list[Check]:
    out = []
    # scoped to the last 7 days: this asks "is the pipeline healthy NOW" — old gaps
    # (e.g. the 2026-07-08 provider outage) are history, not a current failure
    n_v2, n_snap = conn.execute(
        """SELECT COUNT(*),
                  SUM(EXISTS(SELECT 1 FROM graduation_feature_snapshot g
                             WHERE g.token_mint = ge.token_mint))
           FROM graduation_events ge WHERE pipeline_version >= 2
             AND ge.graduated_at > strftime('%s','now') - 86400""").fetchone()
    cov = (n_snap or 0) / max(n_v2, 1)
    # 24h scope: the purge concentrated old outage-era gaps into the surviving
    # classic population; this check asks "is the pipeline healthy NOW"
    out.append(Check("data", "v2 graduations have a snapshot (last 24h)",
                     cov >= 0.90 or n_v2 < 5, f"{cov:.1%} of {n_v2}"))

    bad_ts = conn.execute(
        """SELECT COUNT(*) FROM post_grad_swaps p JOIN graduation_events g USING(token_mint)
           WHERE p.ts < g.graduated_at - 60""").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM post_grad_swaps").fetchone()[0]
    out.append(Check("data", "no tape rows predate their graduation",
                     bad_ts / max(total, 1) < 0.001, f"{bad_ts:,} of {total:,}"))

    fut = conn.execute("SELECT COUNT(*) FROM tokens WHERE created_at > strftime('%s','now') + 300"
                       ).fetchone()[0]
    out.append(Check("data", "no future created_at", fut == 0, f"{fut} rows"))

    # membership gate enforcement on every persisted member row
    from src.analyzer.team_detect import _MEMBER_THRESHOLD, passes_member_gate
    viol = n_mem = 0
    for r in conn.execute("SELECT score, evidence_json FROM team_members WHERE is_member=1"):
        n_mem += 1
        try:
            ev = json.loads(r["evidence_json"] or "{}")
        except Exception:
            ev = {}
        if r["score"] < _MEMBER_THRESHOLD or not passes_member_gate(ev):
            if "fallback_top_holder" not in ev:      # live fallback path is exempt
                viol += 1
    out.append(Check("data", "every is_member row passes the gate",
                     viol / max(n_mem, 1) < 0.02, f"{viol:,} violations of {n_mem:,}"))

    sizes = [r[0] for r in conn.execute(
        "SELECT COUNT(*) FROM team_members WHERE is_member=1 GROUP BY token_mint")]
    mx = max(sizes) if sizes else 0
    out.append(Check("data", "no team exceeds 40 members (bloat regression)",
                     mx <= 40, f"max team size {mx}"))

    # is_team marks on the tape match cluster membership (sample of 100 coins)
    mism = n_chk = 0
    for r in conn.execute(
            """SELECT tc.token_mint, tc.member_addresses FROM team_clusters tc
               JOIN team_members tm ON tm.token_mint = tc.token_mint
               GROUP BY tc.token_mint ORDER BY RANDOM() LIMIT 100"""):
        members = set(json.loads(r["member_addresses"] or "[]"))
        marked = {x[0] for x in conn.execute(
            "SELECT DISTINCT wallet_address FROM post_grad_swaps WHERE token_mint=? AND is_team=1",
            (r["token_mint"],))}
        n_chk += 1
        if not marked <= members:
            mism += 1
    out.append(Check("data", "tape is_team marks ⊆ cluster members (n=100 sample)",
                     mism / max(n_chk, 1) <= 0.05, f"{mism} mismatched coins"))

    # the choreography memory must only learn from gated members (was 88% noise once)
    bad = conn.execute("""SELECT COUNT(*) FROM team_member_behavior b
        WHERE NOT EXISTS (SELECT 1 FROM team_members tm WHERE tm.token_mint=b.token_mint
            AND tm.wallet=b.wallet AND tm.is_member=1)""").fetchone()[0]
    tot_b = conn.execute("SELECT COUNT(*) FROM team_member_behavior").fetchone()[0]
    out.append(Check("data", "team_member_behavior rows are gated members",
                     bad / max(tot_b, 1) < 0.05, f"{bad:,} non-gated of {tot_b:,}"))

    # funder lineage: funding_source must trace to a gated member (was 46% once)
    n_f = ok_f = 0
    for r in conn.execute("""SELECT tc.token_mint, tc.funding_source, tc.member_addresses
        FROM team_clusters tc WHERE tc.funding_source IS NOT NULL
        ORDER BY RANDOM() LIMIT 200"""):
        members = set(json.loads(r["member_addresses"] or "[]"))
        if not members:
            continue
        n_f += 1
        ok_f += bool(conn.execute(
            f"""SELECT 1 FROM wallet_funding WHERE funder=? AND hop=1
                AND wallet IN ({','.join('?'*len(members))}) LIMIT 1""",
            (r["funding_source"], *members)).fetchone())
    out.append(Check("data", "funding_source traces to a gated member (n=200)",
                     ok_f / max(n_f, 1) >= 0.80, f"{ok_f}/{n_f}"))

    # graduated_mints CONTRACT: JSON array. An integer written here once crashed the
    # live verdict path (rules.py does len() on it) for ~90 minutes.
    bad_fmt = conn.execute("""SELECT COUNT(*) FROM funder_reputation
        WHERE NOT (json_valid(graduated_mints) AND json_type(graduated_mints)='array')
        """).fetchone()[0]
    out.append(Check("data", "funder_reputation.graduated_mints is a JSON array",
                     bad_fmt == 0, f"{bad_fmt} malformed rows"))

    # scope: pump.fun only — the ingest gate must hold (foreign venues observed
    # live before the gate: raydium-cpmm)
    foreign = conn.execute("""SELECT COUNT(*) FROM graduation_events
        WHERE graduated_at > strftime('%s','now') - 172800
          AND migration_venue IS NOT NULL AND length(migration_venue) <= 20
          AND lower(migration_venue) NOT IN ('pump-amm','pump')""").fetchone()[0]
    out.append(Check("data", "no non-pump.fun graduations analysed (48h)",
                     foreign == 0, f"{foreign} foreign-venue rows"))

    # PLATFORM gate (the definitive one): every analysed graduation's token must be
    # createdOn pump.fun, or metadata-less with the pump suffix. Mayhem migrates to
    # PumpSwap AND shares the suffix — only createdOn separates it.
    from src.ingest.graduation_monitor import _is_pump_fun_token
    rows48 = conn.execute("""SELECT ge.token_mint m, t.created_on co
        FROM graduation_events ge LEFT JOIN tokens t ON t.mint=ge.token_mint
        WHERE ge.graduated_at > strftime('%s','now') - 172800""").fetchall()
    bad_plat = sum(1 for r in rows48
                   if (r["co"] or "") != "" and not _is_pump_fun_token(r["co"], r["m"]))
    out.append(Check("data", "no non-pump.fun PLATFORM tokens analysed (48h)",
                     bad_plat == 0, f"{bad_plat} of {len(rows48)}"))

    # Mayhem is invisible to metadata — the sweep/gate stamp tokens.launchpad from
    # the creation TX. No analysed graduation may carry a foreign launchpad stamp.
    bad_lp = conn.execute("""SELECT COUNT(*) FROM graduation_events ge
        JOIN tokens t ON t.mint = ge.token_mint
        WHERE ge.graduated_at > strftime('%s','now') - 172800
          AND t.platform IS NOT NULL
          AND t.platform NOT IN ('pump.fun','pump.fun*')""").fetchone()[0]
    out.append(Check("data", "no foreign-launchpad (e.g. mayhem) tokens analysed (48h)",
                     bad_lp == 0, f"{bad_lp} rows"))
    return out


# ── stage 2: label integrity ───────────────────────────────────────────────────────

def stage_labels(conn) -> list[Check]:
    from src.analyzer.trajectory import trajectory_from_db, MIN_TRADES_AT_PEAK, MOON_MULTIPLE
    out = []

    # stored trajectories recompute identically from the tape (mature coins only)
    rows = conn.execute(
        """SELECT ct.token_mint, ge.graduated_at, ct.collapsed, ct.reached_10x,
                  ct.time_to_team_exit_s
           FROM coin_trajectory ct JOIN graduation_events ge USING(token_mint)
           WHERE ct.n_price_points >= 30 AND ge.graduated_at < strftime('%s','now') - 172800
           ORDER BY RANDOM() LIMIT 200""").fetchall()
    diff = 0
    for r in rows:
        t = trajectory_from_db(conn, r["token_mint"], r["graduated_at"])
        if (t.collapsed != r["collapsed"] or t.reached_10x != r["reached_10x"]
                or (t.time_to_team_exit_s or -1) != (r["time_to_team_exit_s"] or -1)):
            diff += 1
    frac = diff / max(len(rows), 1)
    out.append(Check("labels", "stored trajectories == recompute from tape (n=200)",
                     frac <= TRAJ_RECOMPUTE_TOLERANCE, f"{frac:.1%} differ"))

    # the sustain rule actually holds on every reached_10x label
    bad = n10 = 0
    for r in conn.execute(
            """SELECT ct.token_mint, ct.first_price, ge.graduated_at
               FROM coin_trajectory ct JOIN graduation_events ge USING(token_mint)
               WHERE ct.reached_10x=1 AND ct.n_price_points>=30
                 AND ge.graduated_at < strftime('%s','now') - 172800
               ORDER BY RANDOM() LIMIT 100"""):
        n10 += 1
        n_at = conn.execute(
            "SELECT COUNT(*) FROM post_grad_swaps WHERE token_mint=? AND price_usd >= ?",
            (r["token_mint"], MOON_MULTIPLE * r["first_price"])).fetchone()[0]
        if n_at < MIN_TRADES_AT_PEAK:
            bad += 1
    out.append(Check("labels", "every reached_10x is sustained (>=3 prints)",
                     bad == 0, f"{bad} of {n10} violate the sustain rule"))

    # eval loader enforces the thin-tape gate
    tr = _trajectory()
    thin = sum(1 for d in tr.values() if (d.get("n_price_points") or 0) < 30)
    out.append(Check("labels", "eval loader excludes thin tapes (<30 prints)",
                     thin == 0, f"{thin} thin-tape rows leaked into labels"))

    # base rates inside measured bands — the tripwire for silent corruption
    samples = load_samples(conn)
    for head, (lo, hi) in BASE_RATE_BANDS.items():
        ys = [v for s in samples if (v := _label(s, 4, head)) is not None]
        if len(ys) < 300:
            out.append(Check("labels", f"base rate {head}", True,
                             f"SUSPENDED — only {len(ys)} labels (<300); no claim made"))
            continue
        b = float(np.mean(ys))
        out.append(Check("labels", f"base rate {head} in [{lo:.0%},{hi:.0%}]",
                         lo <= b <= hi, f"{b:.1%} (n={len(ys)})"))
    return out


# ── stage 3: leak audit ────────────────────────────────────────────────────────────

def stage_leaks(conn) -> list[Check]:
    out = []
    samples = load_samples(conn)

    ok = sum(1 for s in samples if s.stored_verdict and replay(s.features)[0] == s.stored_verdict)
    n = sum(1 for s in samples if s.stored_verdict)
    fid = ok / max(n, 1)
    out.append(Check("leaks", "rules replay fidelity from frozen snapshots",
                     fid >= REPLAY_FIDELITY_MIN, f"{fid:.2%} of {n}"))

    # forbidden model shapes — negative-results enforcement
    import pickle
    from pathlib import Path
    models = Path(__file__).parent.parent / "models"
    try:
        early = pickle.load(open(models / "early_model_v1.pkl", "rb"))
        heads = set(early["heads"])
        out.append(Check("leaks", "early model has NO pump head (neg. result #1)",
                         heads <= {"survive60"}, f"heads={sorted(heads)}"))
    except FileNotFoundError:
        # post-purge cold start: no head meets the data gate, so no artifact is the
        # CORRECT state — the enforcement applies whenever one exists again
        out.append(Check("leaks", "early model has NO pump head (neg. result #1)",
                         True, "SUSPENDED — no artifact (insufficient data)"))
    try:
        v4 = pickle.load(open(models / "verdict_model_v4.pkl", "rb"))
        leaky = {k for h in v4["heads"].values() for k in h["keys"] if k.startswith("e5_")}
        out.append(Check("leaks", "graduation model uses no post-grad (e5_*) features",
                         not leaky, f"leaked keys: {sorted(leaky) or 'none'}"))
    except FileNotFoundError:
        out.append(Check("leaks", "v4 model artifact present", False, "missing"))

    # single-feature canary: no lone snapshot feature should near-perfectly predict
    # a trajectory label — if one does, the label has leaked into the snapshot
    tr = _trajectory()
    ss = [s for s in samples if s.token_mint in tr]
    worst, worst_name = 0.0, ""
    if len(ss) >= 300:
        keys = feature_names(ss, set())
        X = build_matrix_nan(ss, keys)
        for target in ("survive60", "moon10x"):
            y = np.array([_label(s, 4, target) for s in ss], dtype=float)
            m = ~np.isnan(y)
            for j, k in enumerate(keys):
                col = X[m, j]
                mm = ~np.isnan(col)
                if mm.sum() < 200 or y[m][mm].std() == 0:
                    continue
                r = roc_auc(col[mm], y[m][mm])
                r = max(r, 1 - r)
                if r > worst:
                    worst, worst_name = r, f"{k}→{target}"
    out.append(Check("leaks", f"single-feature ROC canary (< {SINGLE_FEATURE_ROC_MAX})",
                     worst < SINGLE_FEATURE_ROC_MAX, f"worst {worst:.3f} ({worst_name})"))
    return out


# ── stage 4: walk-forward backtest vs bands ────────────────────────────────────────

def stage_backtest(conn) -> list[Check]:
    out = []
    samples = load_samples(conn)
    samples.sort(key=lambda s: s.graduated_at)
    for head, (lo, hi) in ROC_BANDS.items():
        ss = [s for s in samples if _label(s, 4, head) is not None]
        if len(ss) < 500:
            out.append(Check("backtest", f"walk-forward {head}", True,
                             f"SUSPENDED — only {len(ss)} rows (<500); no claim made"))
            continue
        y = np.array([_label(s, 4, head) for s in ss])
        X = build_matrix_nan(ss, feature_names(ss, set()))
        p, yy = _walk_forward(X, y)
        roc = roc_auc(p, yy)
        ok = (lo is None or roc >= lo) and roc <= hi
        why = ("SUSPICIOUS JUMP — audit for a leak before believing it" if roc > hi
               else ("degraded below band" if (lo is not None and roc < lo) else "in band"))
        out.append(Check("backtest", f"{head} ROC in [{lo if lo is not None else '—'}, {hi}]",
                         ok, f"{roc:.3f} (n={len(yy)}) — {why}"))
    return out


# ── stage 5: alert simulation ──────────────────────────────────────────────────────

def stage_alerts(conn) -> list[Check]:
    out = []
    tr = _trajectory()
    samples = [s for s in load_samples(conn)
               if s.token_mint in tr and tr[s.token_mint]["time_to_team_exit_s"] is not None]
    samples.sort(key=lambda s: s.graduated_at)
    if len(samples) < 500:
        out.append(Check("alerts", "alert simulation", True,
                         f"SUSPENDED — only {len(samples)} labeled exits (<500)"))
        return out

    # pre-warning, replayed out-of-time exactly as it would have fired
    y = np.array([float(tr[s.token_mint]["time_to_team_exit_s"] <= 600) for s in samples])
    X = build_matrix_nan(samples, feature_names(samples, set()))
    p, yc = _walk_forward(X, y)
    fired = p >= PREWARN_THRESHOLD
    prec = yc[fired].mean() if fired.sum() >= 25 else float("nan")
    out.append(Check("alerts", f"pre-warn precision @p>={PREWARN_THRESHOLD} >= {PREWARN_PRECISION_MIN:.0%}",
                     bool(fired.sum() >= 25 and prec >= PREWARN_PRECISION_MIN),
                     f"{prec:.1%} on {fired.sum()} fires of {len(yc)}"))
    fr = fired.mean() if len(yc) else 0.0
    out.append(Check("alerts", "pre-warn fire rate in band",
                     PREWARN_FIRE_BAND[0] <= fr <= PREWARN_FIRE_BAND[1], f"{fr:.0%}"))

    # LIVE fire rate at the artifact's own threshold — the raw-vs-calibrated scale
    # mismatch fired on 77% of graduations once; this catches it within a day
    from src.strategy.model_verdict import alert_threshold, artifact_trained_at
    thr = alert_threshold("team_exit10")
    # judge only predictions made by the CURRENT artifact — an older artifact's
    # calibration lives on a different scale and would false-alarm this check
    since = max(artifact_trained_at(), 0)
    row = conn.execute("""SELECT COUNT(*), SUM(p_team_exit10 >= ?) FROM model_predictions
        WHERE p_team_exit10 IS NOT NULL AND predicted_at > ?
          AND predicted_at > strftime('%s','now') - 172800""", (thr, since)).fetchone()
    n_live, n_fired = row[0], row[1] or 0
    if n_live >= 20:
        lr = n_fired / n_live
        out.append(Check("alerts", f"LIVE pre-warn fire rate @artifact thr {thr:.2f} (48h)",
                         PREWARN_FIRE_BAND[0] <= lr <= PREWARN_FIRE_BAND[1],
                         f"{lr:.0%} ({n_fired}/{n_live})"))
    else:
        out.append(Check("alerts", "LIVE pre-warn fire rate (48h)", True,
                         f"only {n_live} live predictions — skipped"))

    # exit alarm: value of selling on the alert vs holding one hour
    after = []
    for r in conn.execute(
            """SELECT ge.token_mint m, ge.graduated_at g, ct.time_to_team_exit_s ex
               FROM graduation_events ge JOIN coin_trajectory ct USING(token_mint)
               WHERE ct.time_to_team_exit_s IS NOT NULL AND ct.n_price_points >= 30
               ORDER BY ge.graduated_at DESC LIMIT 800"""):
        pts = [(x[0] - r["g"], x[1]) for x in conn.execute(
            "SELECT ts, price_usd FROM post_grad_swaps WHERE token_mint=? AND price_usd>0 ORDER BY ts",
            (r["m"],))]
        ex = r["ex"]
        if len(pts) < 30 or pts[-1][0] < ex + 3600:
            continue
        p_alert = next((px for t, px in pts if t >= ex), None)
        held = [px for t, px in pts if t <= ex + 3600]
        if p_alert and held:
            after.append(held[-1] / p_alert)
    a = np.array(after)
    out.append(Check("alerts", f"exit alarm: median 1h-after multiple <= {EXIT_MEDIAN_MAX}",
                     bool(len(a) >= 100 and np.median(a) <= EXIT_MEDIAN_MAX),
                     f"{np.median(a):.2f}x (n={len(a)})"))
    out.append(Check("alerts", f"exit alarm: better off exiting >= {EXIT_BETTER_OFF_MIN:.0%}",
                     bool(len(a) >= 100 and (a < 1).mean() >= EXIT_BETTER_OFF_MIN),
                     f"{(a < 1).mean():.0%}"))
    return out


# ── stage 6: calibration ───────────────────────────────────────────────────────────

def stage_calibration(conn) -> list[Check]:
    out = []
    samples = [s for s in load_samples(conn) if _label(s, 4, "rug") is not None]
    samples.sort(key=lambda s: s.graduated_at)
    if len(samples) < 500:
        return [Check("calibration", "calibration", True,
                      f"SUSPENDED — only {len(samples)} labeled rows (<500)")]
    y = np.array([_label(s, 4, "rug") for s in samples])
    X = build_matrix_nan(samples, feature_names(samples, set()))
    p, yy = _walk_forward(X, y)
    icut = int(len(p) * 0.8)
    cal = platt_fit(p[:icut], yy[:icut])
    pc, yc = platt_apply(cal, p[icut:]), yy[icut:]

    b_model, b_base = brier(pc, yc), brier(np.full_like(yc, yc.mean()), yc)
    out.append(Check("calibration", "calibrated p_rug beats base-rate Brier",
                     b_model < b_base, f"model {b_model:.4f} vs base {b_base:.4f}"))

    worst = 0.0
    for _mid, frac_pred, frac_real, n in calibration_bins(pc, yc):
        if n >= 30:
            worst = max(worst, abs(frac_pred - frac_real))
    out.append(Check("calibration", "per-decile |predicted-realized| <= 0.20 (n>=30 bins)",
                     worst <= 0.20, f"worst gap {worst:.3f}"))
    return out


# ── runner ─────────────────────────────────────────────────────────────────────────

STAGES = [("1 DATA", stage_data), ("2 LABELS", stage_labels), ("3 LEAKS", stage_leaks),
          ("4 BACKTEST", stage_backtest), ("5 ALERTS", stage_alerts),
          ("6 CALIBRATION", stage_calibration)]


def main() -> int:
    quick = "--quick" in sys.argv
    stages = STAGES[:3] if quick else STAGES
    conn = get_connection()
    t0 = time.time()
    checks: list[Check] = []
    for name, fn in stages:
        print(f"\n═══ stage {name} ═══")
        try:
            got = fn(conn)
        except Exception as exc:
            got = [Check(name, "stage crashed", False, f"{type(exc).__name__}: {exc}")]
        for c in got:
            print(f"  {'PASS' if c.ok else '**FAIL**':<10} {c.name:<55} {c.detail}")
        checks.extend(got)

    failed = [c for c in checks if not c.ok]
    print(f"\n{'='*80}\n{len(checks) - len(failed)}/{len(checks)} checks passed "
          f"({time.time()-t0:.0f}s, mode={'quick' if quick else 'full'})")
    if failed:
        print("FAILED:")
        for c in failed:
            print(f"  - [{c.stage}] {c.name}: {c.detail}")

    conn.execute("""CREATE TABLE IF NOT EXISTS backtest_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at INTEGER NOT NULL, mode TEXT,
        n_checks INTEGER, n_failed INTEGER, summary_json TEXT)""")
    conn.execute(
        "INSERT INTO backtest_runs (run_at, mode, n_checks, n_failed, summary_json) VALUES (?,?,?,?,?)",
        (int(time.time()), "quick" if quick else "full", len(checks), len(failed),
         json.dumps([{"stage": c.stage, "name": c.name, "ok": bool(c.ok), "detail": c.detail}
                     for c in checks])))       # bool(): stages 4-6 emit numpy bool_
    conn.commit()
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
