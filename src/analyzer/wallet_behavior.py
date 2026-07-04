"""Per-wallet cross-coin behavioral fingerprints (Phase C).

Structural detection asks "who holds what on THIS coin". Behavioral economics
asks "how does this actor PLAY, across every coin it touches". A wallet's habits
— how fast it snipes, how it sizes, whether it dumps one-shot or bleeds out —
are stable across launches even when the team rotates addresses and funders.

wallet_behavior aggregates those habits from bc_accumulation (BC-phase style)
and post_grad_swaps (exit behavior), recomputed from SQL at the 4h outcome.
The 9-dim normalized vector feeds coordination.edges_behavioral. Gate: only
wallets with n_coins_bc >= 3 are considered non-noise.
"""

import logging
import math
import time

logger = logging.getLogger(__name__)

_MIN_COINS_FOR_SIMILARITY = 3   # habits below this are noise (CLAUDE.md-style gate)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return 0.0 if xs else None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def compute_wallet_behavior(
    accum_rows: list[dict],       # bc_accumulation rows for the wallet
    exit_rows: list[dict],        # per-coin exit summaries (hold_duration_s, one_shot_frac)
    slot_reactions: list[int],    # slot_offset_from_first across coins
    pnl_proxy: float | None,
    sig_count: int | None,
    wallet_age_days: float | None,
) -> dict:
    """Aggregate one wallet's habits into the wallet_behavior row shape (pure)."""
    n = len(accum_rows)
    offsets = [float(r["first_buy_offset_s"]) for r in accum_rows if r.get("first_buy_offset_s") is not None]
    sizes = [float(r["total_sol_in"]) for r in accum_rows if r.get("total_sol_in")]
    styles = [r.get("accumulation_style") for r in accum_rows]
    sniped = sum(1 for s in styles if s == "sniped")
    gradual = sum(1 for s in styles if s == "gradual")
    single = sum(1 for s in styles if s == "single")
    # sniper if styled 'sniped' OR first buy within 120s
    sniper_coins = sum(
        1 for r in accum_rows
        if r.get("accumulation_style") == "sniped"
        or (r.get("first_buy_offset_s") is not None and float(r["first_buy_offset_s"]) <= 120)
    )
    avg_size = _mean(sizes)
    std_size = _std(sizes)
    cv = (std_size / avg_size) if (avg_size and std_size is not None and avg_size > 0) else None

    holds = [float(r["hold_duration_s"]) for r in exit_rows if r.get("hold_duration_s") is not None]
    one_shots = [float(r["one_shot_frac"]) for r in exit_rows if r.get("one_shot_frac") is not None]

    return {
        "n_coins_bc": n,
        "sniper_rate": round(sniper_coins / n, 4) if n else None,
        "avg_first_buy_offset_s": _mean(offsets),
        "std_first_buy_offset_s": _std(offsets),
        "avg_buy_size_sol": avg_size,
        "cv_buy_size": round(cv, 4) if cv is not None else None,
        "pct_sniped": round(sniped / n, 4) if n else None,
        "pct_gradual": round(gradual / n, 4) if n else None,
        "pct_single": round(single / n, 4) if n else None,
        "avg_hold_duration_s": _mean(holds),
        "exit_one_shot_frac": _mean(one_shots),
        "n_coins_exit": len(exit_rows),
        "pnl_proxy": pnl_proxy,
        "avg_slot_reaction": _mean([float(x) for x in slot_reactions]) if slot_reactions else None,
        "sig_count": sig_count,
        "wallet_age_days": wallet_age_days,
    }


def behavior_vector(row: dict) -> tuple[float, ...]:
    """9-dim clipped fingerprint for cosine similarity. Missing fields → 0."""
    def clip(x, hi=1.0):
        return max(0.0, min(x, hi))
    g = lambda k: row.get(k) or 0.0
    return (
        clip(g("sniper_rate")),
        clip(g("avg_first_buy_offset_s") / 300.0),
        clip(g("cv_buy_size") / 2.0),
        clip(g("pct_sniped")),
        clip(g("pct_gradual")),
        clip(g("pct_single")),
        clip(g("avg_hold_duration_s") / 86400.0),
        clip(g("exit_one_shot_frac")),
        clip(math.log10(1 + g("avg_buy_size_sol")) / 2.0),
    )


def update_wallet_behavior(addresses: list[str], conn) -> None:
    """Recompute wallet_behavior for each address from SQL (batched upsert).

    Recompute-from-scratch (not incremental) — each wallet touches few coins, so
    this is cheap and always correct. No awaits inside the write transaction.
    """
    addresses = list(dict.fromkeys(a for a in addresses if a))
    if not addresses:
        return
    now = int(time.time())
    out_rows = []
    for addr in addresses:
        accum = [dict(r) for r in conn.execute(
            """SELECT first_buy_offset_s, total_sol_in, accumulation_style
               FROM bc_accumulation WHERE wallet_address = ?""",
            (addr,),
        )]
        if not accum:
            continue
        exit_rows = _exit_summaries(addr, conn)
        slots = [int(r[0]) for r in conn.execute(
            """SELECT slot_offset_from_first FROM bc_microstructure
               WHERE wallet = ? AND slot_offset_from_first IS NOT NULL""",
            (addr,),
        )]
        ws = conn.execute("SELECT win_rate FROM wallet_stats WHERE address = ?", (addr,)).fetchone()
        pnl = float(ws["win_rate"]) if ws and ws["win_rate"] is not None else None
        wf = conn.execute(
            "SELECT sig_count FROM wallet_funding WHERE wallet = ? AND hop = 1", (addr,)
        ).fetchone()
        sig_count = int(wf["sig_count"]) if wf and wf["sig_count"] is not None else None
        wrow = conn.execute("SELECT first_seen FROM wallets WHERE address = ?", (addr,)).fetchone()
        age_days = ((now - int(wrow["first_seen"])) / 86400.0) if wrow and wrow["first_seen"] else None
        avg_exit_order, n_exit_coins = _exit_order_stats(addr, conn)

        b = compute_wallet_behavior(accum, exit_rows, slots, pnl, sig_count, age_days)
        b["avg_exit_order"] = avg_exit_order
        b["n_coins_exit"] = max(b["n_coins_exit"], int(n_exit_coins))
        out_rows.append((addr, b))

    if not out_rows:
        return
    conn.executemany(
        """INSERT INTO wallet_behavior
               (address, n_coins_bc, sniper_rate, avg_first_buy_offset_s,
                std_first_buy_offset_s, avg_buy_size_sol, cv_buy_size,
                pct_sniped, pct_gradual, pct_single, avg_hold_duration_s,
                exit_one_shot_frac, avg_exit_order, n_coins_exit, pnl_proxy,
                avg_slot_reaction, sig_count, wallet_age_days, last_updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
               n_coins_bc=excluded.n_coins_bc, sniper_rate=excluded.sniper_rate,
               avg_first_buy_offset_s=excluded.avg_first_buy_offset_s,
               std_first_buy_offset_s=excluded.std_first_buy_offset_s,
               avg_buy_size_sol=excluded.avg_buy_size_sol, cv_buy_size=excluded.cv_buy_size,
               pct_sniped=excluded.pct_sniped, pct_gradual=excluded.pct_gradual,
               pct_single=excluded.pct_single, avg_hold_duration_s=excluded.avg_hold_duration_s,
               exit_one_shot_frac=excluded.exit_one_shot_frac, avg_exit_order=excluded.avg_exit_order,
               n_coins_exit=excluded.n_coins_exit, pnl_proxy=excluded.pnl_proxy,
               avg_slot_reaction=excluded.avg_slot_reaction, sig_count=excluded.sig_count,
               wallet_age_days=excluded.wallet_age_days, last_updated=excluded.last_updated""",
        [
            (
                addr, b["n_coins_bc"], b["sniper_rate"], b["avg_first_buy_offset_s"],
                b["std_first_buy_offset_s"], b["avg_buy_size_sol"], b["cv_buy_size"],
                b["pct_sniped"], b["pct_gradual"], b["pct_single"], b["avg_hold_duration_s"],
                b["exit_one_shot_frac"], b["avg_exit_order"], b["n_coins_exit"], b["pnl_proxy"],
                b["avg_slot_reaction"], b["sig_count"], b["wallet_age_days"], now,
            )
            for addr, b in out_rows
        ],
    )
    conn.commit()


def _exit_order_stats(addr: str, conn) -> tuple[float | None, int]:
    """(avg exit_order, coin count) from team_member_behavior (Phase D; empty until then)."""
    try:
        row = conn.execute(
            """SELECT AVG(exit_order), COUNT(*) FROM team_member_behavior
               WHERE wallet = ? AND exit_order IS NOT NULL""",
            (addr,),
        ).fetchone()
    except Exception:
        return None, 0
    if not row:
        return None, 0
    return (float(row[0]) if row[0] is not None else None), int(row[1] or 0)


def _exit_summaries(addr: str, conn) -> list[dict]:
    """Per-coin exit behavior for a wallet from post_grad_swaps + its BC first buy.

    hold_duration_s = first sell ts − first buy ts; one_shot_frac = largest single
    sell / total sold (1.0 = dumped in one tx, →0 = bled out over many).
    """
    mints = [r[0] for r in conn.execute(
        "SELECT DISTINCT token_mint FROM post_grad_swaps WHERE wallet_address = ? AND side='sell'",
        (addr,),
    )]
    out = []
    for mint in mints:
        sells = [dict(r) for r in conn.execute(
            """SELECT token_amount, ts FROM post_grad_swaps
               WHERE token_mint = ? AND wallet_address = ? AND side='sell'""",
            (mint, addr),
        )]
        if not sells:
            continue
        total = sum(float(s["token_amount"]) for s in sells)
        largest = max(float(s["token_amount"]) for s in sells)
        first_sell = min(int(s["ts"]) for s in sells)
        fb = conn.execute(
            "SELECT bought_at FROM token_buyers WHERE token_mint = ? AND wallet_address = ?",
            (mint, addr),
        ).fetchone()
        hold = (first_sell - int(fb["bought_at"])) if fb and fb["bought_at"] else None
        out.append({
            "hold_duration_s": float(hold) if hold is not None and hold >= 0 else None,
            "one_shot_frac": round(largest / total, 4) if total > 0 else None,
        })
    return out


def load_behavior_vectors(addresses: list[str], conn) -> dict[str, tuple[float, ...]]:
    """Vectors for wallets clearing the n_coins_bc>=3 gate — for edges_behavioral."""
    addresses = [a for a in dict.fromkeys(addresses) if a]
    if not addresses:
        return {}
    placeholders = ",".join("?" * len(addresses))
    out = {}
    for r in conn.execute(
        f"""SELECT * FROM wallet_behavior
            WHERE address IN ({placeholders}) AND n_coins_bc >= ?""",
        (*addresses, _MIN_COINS_FOR_SIMILARITY),
    ):
        out[r["address"]] = behavior_vector(dict(r))
    return out
