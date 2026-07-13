"""Post-graduation price/exit TRAJECTORY — the labels that actually matter.

Why this exists: our outcome checks were at 1h/4h/24h, but the measured reality is
that the MEDIAN coin collapses (price < 0.5x) at **10.5 minutes**, and 89.6% are
dead within the hour. Labeling at 1h was measuring the corpse. Meanwhile 25.5% of
coins reach >=10x at some point before dying — an opportunity the coarse labels
made invisible.

So we derive continuous-time labels from the swap tape we already store:

  peak_multiple           — the real "moon" (did it 10x before dying?)
  time_to_peak_s          — when the top was
  time_to_collapse_s      — SURVIVAL target: when does the rug come?
  time_to_team_exit_s     — the LEADING indicator (team sells ~3 min before collapse)
  team_leads_collapse     — did the team get out first? (64% of the time)

All of it is post-graduation OUTCOME data — labels, never features. Nothing here
may leak into graduation_feature_snapshot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

COLLAPSE_FRAC = 0.5      # "rug" = price below half the first post-graduation print
MOON_MULTIPLE = 10.0     # the real moon threshold (not the old 3x checkpoint)

# A peak must be SUSTAINED to count. The raw max is worthless: 78% of coins that
# printed a >=10x max did so on a SINGLE bad trade (one coin printed 2055x once
# while its true peak was 1.11x). Requiring >=3 trades at the level kills the
# artifacts and takes the honest 10x rate from a fake 26% down to ~6%.
MIN_TRADES_AT_PEAK = 3

# Sanity bound — anything past this is a broken price print, not a real move.
MAX_PLAUSIBLE_MULTIPLE = 1000.0


@dataclass
class Trajectory:
    token_mint: str
    first_price: float | None = None
    peak_price: float | None = None
    peak_multiple: float | None = None
    time_to_peak_s: float | None = None
    time_to_collapse_s: float | None = None      # None = never collapsed in tape
    collapsed: int = 0
    reached_10x: int = 0
    time_to_team_exit_s: float | None = None
    team_leads_collapse: int | None = None
    n_price_points: int = 0
    tape_span_s: float | None = None             # how far the tape actually observes


def compute_trajectory(
    token_mint: str,
    priced_swaps: list[tuple[int, float]],   # (ts, price_usd), any order
    graduated_at: int,
    team_first_sell_ts: int | None = None,
) -> Trajectory:
    """Derive trajectory labels from the post-graduation price tape (pure)."""
    t = Trajectory(token_mint=token_mint)
    pts = sorted((ts, p) for ts, p in priced_swaps if p and p > 0 and ts >= graduated_at)
    t.n_price_points = len(pts)
    if not pts:
        return t

    t.tape_span_s = float(pts[-1][0] - graduated_at)
    t.first_price = pts[0][1]
    if t.first_price <= 0:
        return t

    # SUSTAINED peak: the highest level confirmed by >= MIN_TRADES_AT_PEAK prints.
    # (The raw max is dominated by single bad prints — see MIN_TRADES_AT_PEAK.)
    prices_desc = sorted((p for _, p in pts), reverse=True)
    k = min(MIN_TRADES_AT_PEAK, len(prices_desc)) - 1
    peak = prices_desc[k]
    peak = min(peak, t.first_price * MAX_PLAUSIBLE_MULTIPLE)
    peak_ts = next((ts for ts, p in pts if p >= peak), pts[0][0])

    t.peak_price = peak
    t.peak_multiple = round(peak / t.first_price, 4)
    t.time_to_peak_s = float(peak_ts - graduated_at)
    # a real 10x has MANY trades at the level, not one lucky print
    n_at_moon = sum(1 for _, p in pts if p >= MOON_MULTIPLE * t.first_price)
    t.reached_10x = int(n_at_moon >= MIN_TRADES_AT_PEAK)

    for ts, p in pts:
        if p < COLLAPSE_FRAC * t.first_price:
            t.time_to_collapse_s = float(ts - graduated_at)
            t.collapsed = 1
            break

    if team_first_sell_ts is not None:
        t.time_to_team_exit_s = float(team_first_sell_ts - graduated_at)
        if t.time_to_collapse_s is not None:
            t.team_leads_collapse = int(t.time_to_team_exit_s < t.time_to_collapse_s)
    return t


def upsert_trajectory(conn, t: Trajectory) -> None:
    conn.execute(
        """INSERT INTO coin_trajectory
               (token_mint, computed_at, first_price, peak_price, peak_multiple,
                time_to_peak_s, time_to_collapse_s, collapsed, reached_10x,
                time_to_team_exit_s, team_leads_collapse, n_price_points, tape_span_s)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(token_mint) DO UPDATE SET
               computed_at=excluded.computed_at, first_price=excluded.first_price,
               peak_price=excluded.peak_price, peak_multiple=excluded.peak_multiple,
               time_to_peak_s=excluded.time_to_peak_s,
               time_to_collapse_s=excluded.time_to_collapse_s,
               collapsed=excluded.collapsed, reached_10x=excluded.reached_10x,
               time_to_team_exit_s=excluded.time_to_team_exit_s,
               team_leads_collapse=excluded.team_leads_collapse,
               n_price_points=excluded.n_price_points, tape_span_s=excluded.tape_span_s""",
        (t.token_mint, int(time.time()), t.first_price, t.peak_price, t.peak_multiple,
         t.time_to_peak_s, t.time_to_collapse_s, t.collapsed, t.reached_10x,
         t.time_to_team_exit_s, t.team_leads_collapse, t.n_price_points, t.tape_span_s),
    )


def trajectory_from_db(conn, token_mint: str, graduated_at: int) -> Trajectory:
    """Build the trajectory for one mint from the stored post_grad_swaps tape."""
    pts = [(int(r["ts"]), float(r["price_usd"])) for r in conn.execute(
        """SELECT ts, price_usd FROM post_grad_swaps
           WHERE token_mint = ? AND price_usd IS NOT NULL AND price_usd > 0""",
        (token_mint,))]
    row = conn.execute(
        """SELECT MIN(ts) t FROM post_grad_swaps
           WHERE token_mint = ? AND side = 'sell' AND is_team = 1""",
        (token_mint,)).fetchone()
    team_ts = int(row["t"]) if row and row["t"] else None
    return compute_trajectory(token_mint, pts, graduated_at, team_ts)
