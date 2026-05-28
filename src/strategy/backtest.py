"""Replay entry/exit rules against historical my_trades to measure rule PnL."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.strategy.rules import RuleResult, evaluate_rules


@dataclass
class BacktestResult:
    rule_id: str
    trades_triggered: int
    win_rate: float          # fraction of triggered trades that were profitable
    avg_pnl_sol: float       # average PnL in SOL per triggered trade
    total_pnl_sol: float


def load_trades_with_context(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load my_trades joined with token stats into context dicts for rule replay."""
    rows = conn.execute(
        """
        SELECT
            t.tx_signature, t.token_mint, t.side, t.ts,
            t.sol_amount, t.tokens, t.price_sol,
            t.mc_at_entry, t.lp_burned, t.bundle_pct, t.dev_pct,
            t.top10_pct, t.smart_money_in_count_at_entry,
            t.rules_followed, t.exit_reason,
            tok.symbol, tok.launchpad
        FROM my_trades t
        LEFT JOIN tokens tok ON tok.mint = t.token_mint
        ORDER BY t.ts ASC
        """
    ).fetchall()

    contexts: list[dict[str, Any]] = []
    for row in rows:
        ctx = dict(row)
        ctx["lp_burned"] = bool(ctx.get("lp_burned"))
        ctx["rules_followed"] = json.loads(ctx.get("rules_followed") or "[]")
        ctx["smart_money_count"] = ctx.pop("smart_money_in_count_at_entry") or 0
        ctx["minutes_since_launch"] = None  # not stored per-trade
        contexts.append(ctx)
    return contexts


def run_backtest(
    rules: list,
    conn: sqlite3.Connection,
) -> list[BacktestResult]:
    """Evaluate each rule against all historical buy trades and compute PnL stats.

    For each triggered buy, looks for the first subsequent sell of the same token
    to compute realised PnL.
    """
    trades = load_trades_with_context(conn)
    buy_trades = [t for t in trades if t.get("side") == "buy"]
    sell_trades = [t for t in trades if t.get("side") == "sell"]

    results: list[BacktestResult] = []

    for rule in rules:
        rule_id = getattr(rule, "__name__", str(rule))
        pnls: list[float] = []

        for buy in buy_trades:
            rule_result = evaluate_rules([rule], buy)[0]
            if not rule_result.triggered:
                continue

            mint = buy["token_mint"]
            paired_sell = next(
                (s for s in sell_trades if s["token_mint"] == mint and s["ts"] > buy["ts"]),
                None,
            )
            if paired_sell is not None:
                pnls.append(paired_sell["sol_amount"] - buy["sol_amount"])

        results.append(
            BacktestResult(
                rule_id=rule_id,
                trades_triggered=len(pnls),
                win_rate=sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
                avg_pnl_sol=sum(pnls) / len(pnls) if pnls else 0.0,
                total_pnl_sol=sum(pnls),
            )
        )

    return results


def print_backtest_report(results: list[BacktestResult]) -> None:
    """Print a formatted backtest report to stdout."""
    header = f"{'Rule':<25} {'Triggered':>9} {'Win Rate':>9} {'Avg PnL':>9} {'Total PnL':>10}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.rule_id:<25} {r.trades_triggered:>9} "
            f"{r.win_rate * 100:>8.1f}% {r.avg_pnl_sol:>9.3f} {r.total_pnl_sol:>10.3f}"
        )
