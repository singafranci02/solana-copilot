"""Wallet tagging for order-flow capture.

Tags each trading wallet for a token as team / smart_money / known_rugger / new /
unknown, so the live tape and backfill show WHO is buying and selling. Build the
per-mint context once (one DB pass), then tag each wallet in O(1) — critical for
the live handler which sees many trades/second.

Reused by both the live watcher and the historical backfill.
"""

import json
import time
from dataclasses import dataclass, field


# Tag precedence — most specific wins. Documented constant.
TAG_PRECEDENCE = ("team", "known_rugger", "smart_money", "new", "unknown")


@dataclass
class MintTagContext:
    team: set[str] = field(default_factory=set)
    smart_money: set[str] = field(default_factory=set)
    known_rugger: set[str] = field(default_factory=set)
    grad_holders: set[str] = field(default_factory=set)


def tag_wallet(ctx: MintTagContext, wallet: str) -> str:
    """Return the tag for a wallet given a mint context. Pure — no IO."""
    if wallet in ctx.team:
        return "team"
    if wallet in ctx.known_rugger:
        return "known_rugger"
    if wallet in ctx.smart_money:
        return "smart_money"
    if ctx.grad_holders and wallet not in ctx.grad_holders:
        return "new"
    return "unknown"


# ── context building (cached) ───────────────────────────────────────────────────

_SMART_MONEY_TTL = 300   # refresh the global smart-money set every 5 min
_sm_cache: tuple[float, set[str]] | None = None
_mint_cache: dict[str, tuple[float, MintTagContext]] = {}
_MINT_TTL = 1800         # team/grad sets are static post-grad; refresh hourly-ish


def _smart_money_set(conn) -> set[str]:
    global _sm_cache
    now = time.time()
    if _sm_cache and now - _sm_cache[0] < _SMART_MONEY_TTL:
        return _sm_cache[1]
    from src.analyzer.smart_money import get_smart_money_wallets
    s = {w.address for w in get_smart_money_wallets(conn)}
    _sm_cache = (now, s)
    return s


def build_mint_context(conn, mint: str, *, use_cache: bool = True) -> MintTagContext:
    """One DB pass to assemble the tagging context for a mint (cached with TTL)."""
    now = time.time()
    if use_cache and mint in _mint_cache and now - _mint_cache[mint][0] < _MINT_TTL:
        cached = _mint_cache[mint][1]
        # refresh only the slowly-changing global smart-money set
        cached.smart_money = _smart_money_set(conn)
        return cached

    team: set[str] = set()
    known_rugger: set[str] = set()
    cluster = conn.execute(
        """SELECT member_addresses, funding_source FROM team_clusters
           WHERE token_mint = ? LIMIT 1""",
        (mint,),
    ).fetchone()
    if cluster:
        team = set(json.loads(cluster["member_addresses"] or "[]"))
        funder = cluster["funding_source"]
        if funder and funder != "cex":
            rep = conn.execute(
                "SELECT is_known_rugger FROM funder_reputation WHERE funding_source = ?",
                (funder,),
            ).fetchone()
            if rep and rep["is_known_rugger"]:
                # the funder + everyone it funded count as rugger-tagged
                known_rugger = set(team)
                known_rugger.add(funder)

    grad_holders: set[str] = set()
    ge = conn.execute(
        "SELECT bc_top_holders_json FROM graduation_events WHERE token_mint = ?",
        (mint,),
    ).fetchone()
    if ge and ge["bc_top_holders_json"]:
        try:
            grad_holders = {
                h.get("wallet") for h in json.loads(ge["bc_top_holders_json"]) if h.get("wallet")
            }
        except Exception:
            grad_holders = set()

    ctx = MintTagContext(
        team=team,
        smart_money=_smart_money_set(conn),
        known_rugger=known_rugger,
        grad_holders=grad_holders,
    )
    if use_cache:
        _mint_cache[mint] = (now, ctx)
    return ctx


def clear_cache() -> None:
    """Drop cached contexts (e.g. for tests or a watchlist refresh)."""
    global _sm_cache
    _sm_cache = None
    _mint_cache.clear()
