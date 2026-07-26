"""Person-period expansion for discrete-time hazard modeling of team exits.

Turns each coin into one row per time interval it was observed at-risk in, which is
how a duration-with-censoring problem becomes an ordinary supervised problem
(Singer & Willett). Why this is the right shape for our data:

  - the labels ARE durations (time_to_team_exit_s, time_to_collapse_s) with heavy
    right-censoring from short tapes — binary heads at fixed horizons discard both
    the timing information and the censored coins;
  - 231 dense-tape coins expand to ~1,500 interval rows with 168 observed exit
    events, letting a small-n dataset support a calibrated per-interval model;
  - the empirical hazard is wildly non-flat (38% of teams sell in the FIRST 30s,
    then 3-8% per interval, rising again after 20min) — the interval grid below
    matches that shape, fine early and coarse late.

LEAK DISCIPLINE (this project was burned four times; read before touching):
  - static features come from the frozen graduation snapshot only;
  - time-varying covariates for the interval [a, b) are computed from tape trades
    with ts_offset < a STRICTLY — nothing inside the interval being predicted;
  - a coin contributes an interval row only if its tape OBSERVES the full interval
    (span >= b) or the event happens inside the observed part; partially-observed
    event-free intervals are dropped (standard conservative censoring);
  - event = FIRST team sell in [a, b). After the event the coin exits the risk set.
"""

from __future__ import annotations

from dataclasses import dataclass

# Interval grid (seconds post-graduation): fine where the hazard is (median exit
# 2.4 min), coarse in the tail. 14 intervals to 60 min.
DEFAULT_EDGES = (0, 30, 60, 90, 120, 180, 240, 300, 420, 600, 900, 1200, 1800, 2700, 3600)


@dataclass
class PersonPeriod:
    token_mint: str
    interval_idx: int
    t_start: int
    t_end: int
    event: int                      # first team sell landed in this interval
    # time-varying covariates — from tape STRICTLY BEFORE t_start
    tv_trades: float = 0.0          # cumulative trades so far
    tv_wallets: float = 0.0         # cumulative distinct wallets so far
    tv_price_vs_first: float = 1.0  # last price before t_start / first price
    tv_drawdown: float = 0.0        # 1 - (last price / running max before t_start)
    tv_net_flow_recent: float = 0.0 # buy-sell SOL in the PREVIOUS interval only
    tv_log_t: float = 0.0           # log1p(t_start) — baseline hazard shape


def expand_coin(
    token_mint: str,
    tape: list[tuple[int, str, str, float, float]],   # (ts_offset, side, signer, sol, price) sorted
    team: set[str],
    tape_span_s: float,
    exit_offset_s: float | None,
    edges: tuple[int, ...] = DEFAULT_EDGES,
) -> list[PersonPeriod]:
    """One coin -> its at-risk interval rows. Pure; leak-free by construction."""
    rows: list[PersonPeriod] = []
    first_price = next((p for _, _, _, _, p in tape if p and p > 0), None)

    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        if exit_offset_s is not None and exit_offset_s < a:
            break                                    # event already happened — off risk set
        event_here = exit_offset_s is not None and a <= exit_offset_s < b
        if not event_here and tape_span_s < b:
            break                                    # interval not fully observed — censor

        pre = [x for x in tape if x[0] < a]          # STRICTLY before interval start
        prev = [x for x in pre if x[0] >= (edges[i - 1] if i > 0 else 0)] if i > 0 else []

        pp = PersonPeriod(token_mint=token_mint, interval_idx=i,
                          t_start=a, t_end=b, event=int(event_here))
        pp.tv_trades = float(len(pre))
        pp.tv_wallets = float(len({x[2] for x in pre if x[2]}))
        import math
        pp.tv_log_t = math.log1p(a)
        if first_price and pre:
            priced = [p for _, _, _, _, p in pre if p and p > 0]
            if priced:
                last = priced[-1]
                pp.tv_price_vs_first = min(last / first_price, 50.0)
                run_max = max(priced)
                pp.tv_drawdown = max(0.0, 1.0 - last / run_max) if run_max > 0 else 0.0
        if prev:
            buys = sum(s or 0.0 for _, side, _, s, _ in prev if side == "buy")
            sells = sum(s or 0.0 for _, side, _, s, _ in prev if side == "sell")
            pp.tv_net_flow_recent = buys - sells
        rows.append(pp)
        if event_here:
            break
    return rows


def load_person_periods(conn, edges: tuple[int, ...] = DEFAULT_EDGES,
                        min_price_points: int = 30):
    """Expand every verified-clean dense-tape coin. Returns (rows, static_features).

    static_features: {token_mint: frozen-snapshot feature dict} for joining the
    graduation-time covariates. Population filters mirror eval/_common.load_samples.
    """
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

    all_rows, statics, grad_order = [], {}, []
    for c in coins:
        tape = [(int(x["ts"]) - c["g"], x["side"], x["wallet_address"],
                 float(x["sol_amount"] or 0.0), float(x["price_usd"] or 0.0))
                for x in conn.execute(
                    """SELECT ts, side, wallet_address, sol_amount, price_usd
                       FROM post_grad_swaps WHERE token_mint = ? ORDER BY ts""", (c["m"],))
                if int(x["ts"]) >= c["g"]]
        rows = expand_coin(c["m"], tape, teams.get(c["m"], set()),
                           float(c["span"] or 0.0), c["tex"], edges)
        if rows:
            all_rows.extend(rows)
            try:
                statics[c["m"]] = json.loads(c["fj"] or "{}")
            except Exception:
                statics[c["m"]] = {}
            grad_order.append((c["m"], c["g"]))
    return all_rows, statics, grad_order
