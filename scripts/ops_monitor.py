"""Ops monitor — intraday health controller. Texts Telegram when something NEEDS FIXING.

Complements (does not duplicate) the nightly correctness audit: the audit asks "are
the numbers right"; this asks "is the machine healthy RIGHT NOW" — every 15 minutes,
so a 9am breakage is a 9:15am text, not a next-day discovery.

Checks (each returns ok, detail):
  feed_alive        graduations arriving at a plausible rate for recent flow
  gate_backlog      unverified coins not piling up (re-resolver alive)
  shadow_scoring    v5 hazard predictions emitted for recent graduations
  artifact_loads    hazard + telegram config present and loadable
  db_writable       SQLite accepts a write (WAL not wedged)
  disk_space        >= 15 GB free (DB is 10GB and grows)
  st_budget         Solana Tracker monthly pace within budget
  machine_awake     no sleep events since last run
  services_loaded   all launchd jobs present

ANTI-SPAM: state persisted in ops_state; each failing check alerts at most once per
COOLDOWN_H, re-alerts only if still failing after the cooldown, and sends a single
recovery note when it heals. All failures batch into ONE message.

    uv run python scripts/ops_monitor.py           # launchd: every 15 min
"""

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection

COOLDOWN_H = 4
EXPECTED_SERVICES = (
    "com.solana-copilot.graduation-monitor", "com.solana-copilot.pump-monitor",
    "com.solana-copilot.analyzer", "com.solana-copilot.wallet-watcher",
    "com.solana-copilot.caffeinate", "com.solana-copilot.reresolve",
)
ST_MONTHLY_BUDGET = 200_000


# ── pure check helpers (unit-tested) ──────────────────────────────────────────────

def feed_gap_is_alarming(minutes_since_last: float, grads_last_24h: int) -> bool:
    """A gap is alarming relative to recent flow: at 30/day a 4h gap is ~5x the mean
    inter-arrival; when flow is already near-zero, only alarm after 12h (quiet market
    vs dead feed is undecidable sooner — the WS watchdog covers the socket itself)."""
    if grads_last_24h >= 10:
        return minutes_since_last > 240
    return minutes_since_last > 720


def st_plan_anchor_day() -> int:
    """Day of month the Solana Tracker plan renews (ST_PLAN_ANCHOR_DAY, default 1).

    The billing cycle runs from the purchase date, not from the 1st. Counting the
    calendar month charges the new plan for the previous one's usage: after the
    2026-08-10 repurchase the check read 185,010 of 200,000 and alarmed, while the
    actual spend on the live plan was 1,900.
    """
    from src.common.config import settings
    d = int(settings.st_plan_anchor_day or 1)
    return d if 1 <= d <= 28 else 1


def st_pace_exceeds_budget(calls_this_cycle: int, days_elapsed: int) -> bool:
    """Projected cycle-end usage > budget, with 3 days of grace after renewal."""
    if days_elapsed < 3:
        return False
    projected = calls_this_cycle / days_elapsed * 30
    return projected > ST_MONTHLY_BUDGET


def should_alert(state: dict | None, ok: bool, now: int,
                 cooldown_s: int = COOLDOWN_H * 3600) -> tuple[str | None, dict]:
    """Dedup state machine. Returns (action, new_state); action in
    {'alert','realert','recovered',None}."""
    prev_ok = state is None or bool(state.get("ok", 1))
    last_alert = int(state.get("last_alerted_at", 0)) if state else 0
    if not ok and prev_ok:
        return "alert", {"ok": 0, "last_alerted_at": now}
    if not ok and not prev_ok:
        if now - last_alert >= cooldown_s:
            return "realert", {"ok": 0, "last_alerted_at": now}
        return None, {"ok": 0, "last_alerted_at": last_alert}
    if ok and not prev_ok:
        return "recovered", {"ok": 1, "last_alerted_at": last_alert}
    return None, {"ok": 1, "last_alerted_at": last_alert}


# ── checks ────────────────────────────────────────────────────────────────────────

def run_checks(conn) -> list[tuple[str, bool, str]]:
    now = int(time.time())
    out: list[tuple[str, bool, str]] = []

    last = conn.execute("SELECT MAX(graduated_at) FROM graduation_events").fetchone()[0] or 0
    g24 = conn.execute("SELECT COUNT(*) FROM graduation_events WHERE graduated_at > ?",
                       (now - 86400,)).fetchone()[0]
    gap_m = (now - last) / 60
    out.append(("feed_alive", not feed_gap_is_alarming(gap_m, g24),
                f"last graduation {gap_m:.0f}m ago ({g24} in 24h)"))

    # Backlog scales with VOLUME: a fixed "5" was calibrated at ~23 coins/day and
    # flapped constantly once capture rose. Threshold = 20% of 24h intake (min 15),
    # with HYSTERESIS (clear at 60% of the alarm level) so it cannot oscillate.
    backlog = conn.execute("""SELECT COUNT(*) FROM graduation_events ge
        LEFT JOIN tokens t ON t.mint = ge.token_mint
        WHERE (t.platform IS NULL OR t.platform = 'unverified')
          AND ge.graduated_at > ?""", (now - 86400,)).fetchone()[0]
    alarm_at = max(15, int(0.20 * g24))
    prev = conn.execute("SELECT ok FROM ops_state WHERE check_name='gate_backlog'").fetchone()
    currently_alarmed = prev is not None and not prev["ok"]
    threshold = int(alarm_at * 0.6) if currently_alarmed else alarm_at
    out.append(("gate_backlog", backlog <= threshold,
                f"{backlog} unverified (24h) vs limit {threshold}"))

    recent_grads = conn.execute("""SELECT COUNT(*) FROM graduation_events ge
        JOIN tokens t ON t.mint = ge.token_mint
        WHERE t.platform = 'pump.fun' AND ge.graduated_at BETWEEN ? AND ?""",
        (now - 21600, now - 900)).fetchone()[0]
    scored = conn.execute("SELECT COUNT(DISTINCT token_mint) FROM hazard_predictions "
                          "WHERE scored_at > ?", (now - 21600,)).fetchone()[0]
    out.append(("shadow_scoring", recent_grads == 0 or scored > 0,
                f"{scored} coins scored vs {recent_grads} verified grads (6h)"))

    # CORE DEPENDENCY: a Solana Tracker 401/403 silently breaks team detection for
    # every coin. Detect it directly from the analysis failure signature rather than
    # waiting for the generic consecutive-failure watchdog.
    try:
        import subprocess as _sp
        log = Path(__file__).parent.parent / "logs" / "graduation_monitor.err"
        recent = _sp.run(["tail", "-300", str(log)], capture_output=True,
                         text=True, timeout=15).stdout if log.exists() else ""
        auth_fail = recent.count("401") + recent.count("Invalid API key")
        out.append(("data_source_auth", auth_fail < 3,
                    f"{auth_fail} auth failures in recent log"
                    + (" — CHECK SOLANA TRACKER KEY/SUBSCRIPTION" if auth_fail >= 3 else "")))
    except Exception:
        out.append(("data_source_auth", True, "log unreadable — skipped"))

    try:
        from src.strategy.hazard_verdict import _load
        art = _load()
        from src.common.config import settings
        tg = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        out.append(("artifact_loads", art is not None and tg,
                    f"hazard={'ok' if art else 'MISSING'} telegram={'ok' if tg else 'MISSING'}"))
    except Exception as exc:
        out.append(("artifact_loads", False, f"loader raised: {type(exc).__name__}"))

    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ops_probe (x INTEGER)")
        conn.execute("INSERT INTO ops_probe VALUES (1)")
        conn.execute("DELETE FROM ops_probe")
        conn.commit()
        out.append(("db_writable", True, "write ok"))
    except Exception as exc:
        out.append(("db_writable", False, f"{type(exc).__name__}"))

    free_gb = shutil.disk_usage("/").free / 1e9
    out.append(("disk_space", free_gb >= 15, f"{free_gb:.0f} GB free"))

    # LOG BLOAT. A service stuck in a failing retry loop writes megabytes a minute
    # and nothing else notices: wallet_watcher reached 7.9 GB of consecutive
    # rate-limit lines before anyone looked. Disk headroom alone missed it — 212 GB
    # were still free while the service had been dead for days, so the size of a
    # single log is the signal, not the free space.
    logs = Path(__file__).parent.parent / "logs"
    biggest, big_mb = None, 0.0
    for f in logs.glob("*.err") if logs.exists() else []:
        mb = f.stat().st_size / 1e6
        if mb > big_mb:
            biggest, big_mb = f.name, mb
    out.append(("log_bloat", big_mb < 500,
                f"largest log {biggest or 'none'} at {big_mb:.0f} MB"))

    # Cycle start = the most recent occurrence of the plan's anchor day.
    anchor = st_plan_anchor_day()
    row = conn.execute("""
        WITH c(start) AS (SELECT CASE
                WHEN CAST(strftime('%d','now') AS INT) >= :a
                THEN date('now','start of month','+' || (:a - 1) || ' days')
                ELSE date('now','start of month','-1 month','+' || (:a - 1) || ' days')
            END)
        SELECT COALESCE(SUM(u.count),0),
               CAST(julianday('now') - julianday((SELECT start FROM c)) AS INT) + 1,
               (SELECT start FROM c)
        FROM api_usage u
        WHERE u.provider='solana_tracker' AND u.day >= (SELECT start FROM c)""",
        {"a": anchor}).fetchone()
    out.append(("st_budget", not st_pace_exceeds_budget(row[0], row[1]),
                f"{row[0]:,} calls this cycle (day {row[1]}, since {row[2]})"))

    try:
        slept = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True,
                               timeout=20).stdout.count("Entering Sleep")
        prev = conn.execute("SELECT detail FROM ops_state WHERE check_name='_sleep_count'"
                            ).fetchone()
        prev_n = int(prev["detail"]) if prev and (prev["detail"] or "").isdigit() else slept
        conn.execute("""INSERT INTO ops_state (check_name, ok, last_alerted_at, detail)
            VALUES ('_sleep_count', 1, 0, ?) ON CONFLICT(check_name) DO UPDATE SET
            detail=excluded.detail""", (str(slept),))
        conn.commit()
        out.append(("machine_awake", slept <= prev_n, f"{slept - prev_n} new sleep events"))
    except Exception:
        out.append(("machine_awake", True, "pmset unavailable — skipped"))

    # REJECTION-RATE PLAUSIBILITY: a gate silently rejecting almost everything in a
    # category is the quiet-failure class that discarded 330 real coins (the metadata
    # gate demanded a literal string ST does not stably return). Mayhem legitimately
    # dominates, so this watches only NON-mayhem rejections: if we reject far more
    # non-mayhem coins than we accept, the gate is probably wrong again.
    rej = conn.execute("""SELECT COUNT(*) FROM skipped_graduations
        WHERE skipped_at > ? AND COALESCE(created_on,'') != 'mayhem'""",
        (now - 86400,)).fetchone()[0]
    acc = conn.execute("SELECT COUNT(*) FROM graduation_events WHERE graduated_at > ?",
                       (now - 86400,)).fetchone()[0]
    ratio = rej / max(acc, 1)
    out.append(("rejection_plausible", ratio <= 1.5 or acc < 5,
                f"{rej} non-mayhem rejected vs {acc} accepted (24h, ratio {ratio:.1f})"))

    try:
        import os
        art = Path(__file__).parent.parent / "models" / "hazard_model_v5.pkl"
        age_d = (now - art.stat().st_mtime) / 86400 if art.exists() else 999
        out.append(("artifact_fresh", age_d <= 8.5,
                    f"hazard artifact {age_d:.1f}d old (weekly retrain due <=8d)"))
    except Exception:
        out.append(("artifact_fresh", True, "stat unavailable — skipped"))

    try:
        loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                                timeout=20).stdout
        missing = [s for s in EXPECTED_SERVICES if s not in loaded]
        out.append(("services_loaded", not missing, f"missing: {missing or 'none'}"))
    except Exception:
        out.append(("services_loaded", True, "launchctl unavailable — skipped"))
    return out


def main() -> int:
    conn = get_connection()
    conn.execute("""CREATE TABLE IF NOT EXISTS ops_state (
        check_name TEXT PRIMARY KEY, ok INTEGER NOT NULL DEFAULT 1,
        last_alerted_at INTEGER NOT NULL DEFAULT 0, detail TEXT)""")
    conn.commit()
    now = int(time.time())

    alerts, recoveries = [], []
    for name, ok, detail in run_checks(conn):
        row = conn.execute("SELECT ok, last_alerted_at FROM ops_state WHERE check_name=?",
                           (name,)).fetchone()
        state = dict(row) if row else None
        action, new = should_alert(state, ok, now)
        conn.execute("""INSERT INTO ops_state (check_name, ok, last_alerted_at, detail)
            VALUES (?,?,?,?) ON CONFLICT(check_name) DO UPDATE SET ok=excluded.ok,
            last_alerted_at=excluded.last_alerted_at, detail=excluded.detail""",
            (name, new["ok"], new["last_alerted_at"], detail))
        if action in ("alert", "realert"):
            alerts.append(f"  • <b>{name}</b>: {detail}")
        elif action == "recovered":
            recoveries.append(f"  • {name}: {detail}")
    conn.commit()
    conn.close()

    from src.notifications.telegram import send_message
    if alerts:
        asyncio.run(send_message(
            "🔧 <b>OPS — needs fixing</b>\n" + "\n".join(alerts)
            + "\n<i>re-alerts at most every "
            + f"{COOLDOWN_H}h; a recovery note follows when healed</i>"))
        print(f"alerted: {len(alerts)} issue(s)")
        return 1
    if recoveries:
        asyncio.run(send_message("✅ <b>OPS — recovered</b>\n" + "\n".join(recoveries)))
        print(f"recovered: {len(recoveries)}")
    else:
        print("all healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
