"""Person-period expansion for competing-risks discrete-time hazard models (v5).

Two coupled models over one pre-registered interval grid (spec: adversarially
reviewed, see docs/V5_DESIGN.md):

  MODEL A (team exit): risk set = coins whose gated team has not sold AND that
  have not collapsed. Event = first gated-team sell. Collapse removes a coin from
  A's risk set as a COMPETING event, never silent censoring.

  MODEL B (collapse): risk set = coins not yet collapsed. Event = confirmed
  sustained collapse (delayed stopping time, below). Carries team_exited and
  time-since-exit as time-varying covariates; evaluated at team_exited=0 it
  supplies the death-without-exit hazard h_D for the competing-risks CIF.

THE GRID: sorted union of the analysis grid and the live checkpoint cadence
(EARLY_CHECK_SECONDS), truncated at 3600s (administrative censoring). Every live
checkpoint is an exact interval boundary, so landmark scoring never straddles an
edge. The open 60min+ interval is deliberately absent: under the censoring rule an
unbounded interval can only ever receive event rows, so its hazard degenerates.

COLLAPSE EVENT (delayed stopping time — NOT trajectory.py's whole-tape mirror,
which backdates collapse across genuine recoveries):
  anchor    = median of the first k>=3 priced prints (a lone first print re-admits
              the single-bad-print artifact this project was burned by);
  candidate = first print < 0.5 * anchor;
  confirmed = >=3 prints < 0.5 * anchor within W seconds of the candidate;
  voided    = a sustained recovery (>=3 prints >= 0.5 * anchor) lands before
              confirmation completes -> candidate dropped, search restarts after
              the recovery. Decidable with <= W lookahead; event stamped at the
              CANDIDATE time with confirmations local to it (auditable).

LEAK DISCIPLINE (unchanged, enforced by tests): time-varying covariates for the
interval [a,b) come from tape trades with ts_offset < a STRICTLY; event-free rows
require the tape to observe the full interval; the 2026-07-22..24 ingest-truncated
burst is quarantined from the label-of-record population.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Pre-registered grid: union of the original analysis edges and the live
# EARLY_CHECK_SECONDS cadence (120,210,300,390,480,600,720,900,1200,2400).
GRID_EDGES = (0, 30, 60, 90, 120, 180, 210, 240, 300, 390, 420, 480, 600, 720,
              900, 1200, 1800, 2400, 2700, 3600)
T_ADMIN = 3600                      # administrative censoring horizon (s)

COLLAPSE_FRAC = 0.5
ANCHOR_K = 3                        # prints in the robust first-price anchor
CONFIRM_N = 3                       # sub-threshold prints needed to confirm
CONFIRM_W = 120                     # confirmation window (s) — sensitivity axis
RECOVER_N = 3                       # prints >= threshold that void a candidate

# ingest-truncated burst: quarantined from labels regardless of censoring model
QUARANTINE_UTC = (1784764800, 1785024000)          # 2026-07-23 00:00 .. 07-26 00:00


def robust_anchor(prices: list[tuple[int, float]], k: int = ANCHOR_K) -> float | None:
    """Median of the first k priced prints; None if fewer than k exist."""
    px = [p for _, p in prices[: k]]
    if len(px) < k:
        return None
    return float(sorted(px)[len(px) // 2])


def collapse_event(prices: list[tuple[int, float]], w: int = CONFIRM_W,
                   anchor_k: int = ANCHOR_K) -> float | None:
    """Delayed-stopping-time collapse offset (s), or None. Pure.

    prices: (ts_offset, price) sorted, positive prices only."""
    anchor = robust_anchor(prices, anchor_k)
    if anchor is None or anchor <= 0:
        return None
    thr = COLLAPSE_FRAC * anchor
    i = 0
    n = len(prices)
    while i < n:
        if prices[i][1] >= thr:
            i += 1
            continue
        cand_t = prices[i][0]
        confirms = 0
        recovers = 0
        j = i
        voided_at = None
        while j < n and prices[j][0] <= cand_t + w:
            if prices[j][1] < thr:
                confirms += 1
                recovers = 0
                if confirms >= CONFIRM_N:
                    return float(cand_t)
            else:
                recovers += 1
                if recovers >= RECOVER_N:
                    voided_at = j
                    break
            j += 1
        if voided_at is not None:
            i = voided_at + 1                     # restart after the recovery
        else:
            i = j if j > i else i + 1             # window exhausted unconfirmed
    return None


@dataclass
class PersonPeriod:
    token_mint: str
    interval_idx: int
    t_start: int
    t_end: int
    event: int
    # base time-varying covariates — from tape STRICTLY BEFORE t_start
    tv_trades: float = 0.0
    tv_wallets: float = 0.0
    tv_price_vs_first: float = 1.0     # last robust price / robust anchor
    tv_drawdown: float = 0.0
    tv_net_flow_recent: float = 0.0
    tv_log_t: float = 0.0
    # model-B extras (0 for A-rows)
    b_team_exited: float = 0.0
    b_log_t_since_exit: float = 0.0
    b_team_sellers: float = 0.0
    b_log_prints: float = 0.0


def _tv(pp: PersonPeriod, pre, prev, anchor: float | None) -> None:
    pp.tv_trades = float(len(pre))
    pp.tv_wallets = float(len({x[2] for x in pre if x[2]}))
    pp.tv_log_t = math.log1p(pp.t_start)
    priced = [p for _, _, _, _, p in pre if p and p > 0]
    if anchor and priced:
        last = priced[-1]
        pp.tv_price_vs_first = min(last / anchor, 50.0)
        run_max = max(priced)
        pp.tv_drawdown = max(0.0, 1.0 - last / run_max) if run_max > 0 else 0.0
    if prev:
        buys = sum(s or 0.0 for _, side, _, s, _ in prev if side == "buy")
        sells = sum(s or 0.0 for _, side, _, s, _ in prev if side == "sell")
        pp.tv_net_flow_recent = buys - sells
    pp.b_log_prints = math.log1p(len(priced))


def expand_coin(
    token_mint: str,
    tape: list[tuple[int, str, str, float, float]],   # (ts, side, signer, sol, price)
    team: set[str],
    tape_span_s: float,
    exit_offset_s: float | None,
    edges: tuple[int, ...] = GRID_EDGES,
    confirm_w: int = CONFIRM_W,
) -> tuple[list[PersonPeriod], list[PersonPeriod], float | None]:
    """One coin -> (A_rows, B_rows, collapse_offset). Pure; leak-free.

    A-rows: at risk of FIRST TEAM SELL; competing collapse removes from risk set.
    B-rows: at risk of COLLAPSE; team_exited/time-since-exit as TV covariates."""
    prices = [(t, p) for t, _, _, _, p in tape if p and p > 0]
    anchor = robust_anchor(prices)
    coll = collapse_event(prices, confirm_w) if anchor else None
    span = min(float(tape_span_s), float(T_ADMIN))

    a_rows: list[PersonPeriod] = []
    b_rows: list[PersonPeriod] = []
    team_sells = sorted(t for t, side, sg, _, _ in tape if side == "sell" and sg in team)

    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        pre = [x for x in tape if x[0] < a]
        prev = [x for x in pre if x[0] >= edges[i - 1]] if i > 0 else []

        # ── model A row: team not exited, not collapsed, interval observed
        a_alive = ((exit_offset_s is None or exit_offset_s >= a)
                   and (coll is None or coll >= a))
        a_event = (exit_offset_s is not None and a <= exit_offset_s < b
                   and (coll is None or exit_offset_s <= coll))
        a_competing = coll is not None and a <= coll < b and not a_event
        if a_alive and (a_event or a_competing or span >= b):
            pp = PersonPeriod(token_mint, i, a, b, int(a_event))
            _tv(pp, pre, prev, anchor)
            if not a_competing or a_event:
                a_rows.append(pp)
            if a_event or a_competing:
                pass                                 # falls off A's risk set after
        if a_event or a_competing:
            a_alive = False

        # ── model B row: not collapsed, interval observed
        b_alive = coll is None or coll >= a
        b_event = coll is not None and a <= coll < b
        if b_alive and (b_event or span >= b):
            pb = PersonPeriod(token_mint, i, a, b, int(b_event))
            _tv(pb, pre, prev, anchor)
            exited = exit_offset_s is not None and exit_offset_s < a
            pb.b_team_exited = float(exited)
            pb.b_log_t_since_exit = math.log1p(a - exit_offset_s) if exited else 0.0
            pb.b_team_sellers = float(len({sg for t, side, sg, _, _ in tape
                                           if side == "sell" and sg in team and t < a}))
            b_rows.append(pb)
        if (exit_offset_s is not None and exit_offset_s < b) and (coll is None or coll >= b):
            pass
        if b_event:
            break                                    # both risk sets end at collapse
        if not a_alive and (coll is None or coll >= b) and span < b:
            break
    return a_rows, b_rows, coll


def landmark_row(
    tape: list[tuple[int, str, str, float, float]],
    team: set[str],
    checkpoint_s: int,
    edges: tuple[int, ...] = GRID_EDGES,
) -> PersonPeriod | None:
    """The single person-period row for the interval STARTING at a checkpoint.

    Live scoring helper: covariates from tape STRICTLY before the checkpoint (which
    is a grid edge by construction — the grid is the union of the analysis edges and
    EARLY_CHECK_SECONDS). Returns None if the checkpoint is not a grid edge or is
    the terminal edge. Exit state is derived from observed team sells before the
    checkpoint, so the caller needs no external exit bookkeeping."""
    if checkpoint_s not in edges or checkpoint_s == edges[-1]:
        return None
    i = edges.index(checkpoint_s)
    a, b = edges[i], edges[i + 1]
    pre = [x for x in tape if x[0] < a]
    prev = [x for x in pre if x[0] >= edges[i - 1]] if i > 0 else []
    prices = [(t, pz) for t, _, _, _, pz in tape if pz and pz > 0]
    anchor = robust_anchor(prices)
    pp = PersonPeriod("", i, a, b, 0)
    _tv(pp, pre, prev, anchor)
    team_sells = sorted(t for t, side, sg, _, _ in pre if side == "sell" and sg in team)
    if team_sells:
        pp.b_team_exited = 1.0
        pp.b_log_t_since_exit = math.log1p(a - team_sells[0])
        pp.b_team_sellers = float(len({sg for t, side, sg, _, _ in pre
                                       if side == "sell" and sg in team}))
    return pp


def load_person_periods(conn, edges: tuple[int, ...] = GRID_EDGES,
                        min_price_points: int = 30, confirm_w: int = CONFIRM_W,
                        quarantine: bool = True):
    """Expand every verified-clean dense-tape coin -> (A_rows, B_rows, statics, order)."""
    import json
    coins = [dict(r) for r in conn.execute("""
        SELECT ct.token_mint m, ge.graduated_at g, ct.time_to_team_exit_s tex,
               ct.tape_span_s span, gfs.features_json fj
        FROM coin_trajectory ct
        JOIN graduation_events ge ON ge.token_mint = ct.token_mint
        JOIN tokens t ON t.mint = ct.token_mint
        JOIN graduation_feature_snapshot gfs ON gfs.token_mint = ct.token_mint
        WHERE t.platform = 'pump.fun' AND COALESCE(ge.is_manufactured, 0) = 0
          AND ct.n_price_points >= ?
        ORDER BY ge.graduated_at""", (min_price_points,))]

    teams = {}
    for r in conn.execute("SELECT token_mint, member_addresses FROM team_clusters"):
        try:
            teams[r["token_mint"]] = set(json.loads(r["member_addresses"] or "[]"))
        except Exception:
            teams[r["token_mint"]] = set()

    A, B, statics, order = [], [], {}, []
    for c in coins:
        if quarantine and QUARANTINE_UTC[0] <= c["g"] < QUARANTINE_UTC[1] \
                and (c["span"] or 0) < T_ADMIN:
            continue                     # ingest-truncated burst: not label-of-record
        tape = [(int(x["ts"]) - c["g"], x["side"], x["wallet_address"],
                 float(x["sol_amount"] or 0.0), float(x["price_usd"] or 0.0))
                for x in conn.execute(
                    """SELECT ts, side, wallet_address, sol_amount, price_usd
                       FROM post_grad_swaps WHERE token_mint = ? ORDER BY ts""", (c["m"],))
                if int(x["ts"]) >= c["g"]]
        a_rows, b_rows, _ = expand_coin(c["m"], tape, teams.get(c["m"], set()),
                                        float(c["span"] or 0.0), c["tex"], edges, confirm_w)
        if a_rows or b_rows:
            A.extend(a_rows)
            B.extend(b_rows)
            try:
                statics[c["m"]] = json.loads(c["fj"] or "{}")
            except Exception:
                statics[c["m"]] = {}
            order.append((c["m"], c["g"]))
    return A, B, statics, order
