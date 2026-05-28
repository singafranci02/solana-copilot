"""Entry and exit rules — the file you edit between sessions.

Each rule is a function that takes a structured context dict and returns
a bool (should I act?) plus an optional reason string.

Design principles:
  - Rules are pure functions: no IO, no side effects.
  - Add new rules by writing a new function and registering it in ENTRY_RULES
    or EXIT_RULES at the bottom of this file.
  - backtest.py replays these rules against my_trades to measure their PnL.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class RuleResult:
    triggered: bool
    rule_id: str
    reason: str


def rule_smart_money_early(ctx: dict[str, Any]) -> RuleResult:
    """Trigger when 2+ smart money wallets bought in the first 5 minutes."""
    count = int(ctx.get("smart_money_count") or 0)
    mins = float(ctx.get("minutes_since_launch") if ctx.get("minutes_since_launch") is not None else float("inf"))
    triggered = count >= 2 and mins <= 5
    reason = (
        f"{count} SM wallet(s) bought within {mins:.1f} min of launch"
        if triggered
        else f"conditions not met: count={count}, mins_since_launch={mins:.1f}"
    )
    return RuleResult(triggered=triggered, rule_id="smart_money_early", reason=reason)


def rule_low_bundle(ctx: dict[str, Any]) -> RuleResult:
    """Trigger when bundle_pct < 10 (low insider coordination at launch)."""
    bundle_pct = ctx.get("bundle_pct")
    if bundle_pct is None:
        return RuleResult(triggered=False, rule_id="low_bundle", reason="bundle_pct unknown")
    bundle_pct = float(bundle_pct)
    triggered = bundle_pct < 10
    return RuleResult(
        triggered=triggered,
        rule_id="low_bundle",
        reason=f"bundle_pct={bundle_pct:.1f}% {'<' if triggered else '>='} 10%",
    )


def rule_lp_burned(ctx: dict[str, Any]) -> RuleResult:
    """Trigger when liquidity pool is burned (rug risk reduced)."""
    burned = bool(ctx.get("lp_burned"))
    return RuleResult(
        triggered=burned,
        rule_id="lp_burned",
        reason="LP burned" if burned else "LP not burned",
    )


def rule_narrative_hot(ctx: dict[str, Any]) -> RuleResult:
    """Trigger when token matches a narrative with velocity > 50 mentions/hour."""
    narratives: list[str] = ctx.get("matched_narratives") or []
    velocities: dict[str, float] = ctx.get("narrative_velocities") or {}
    for label in narratives:
        v = float(velocities.get(label, 0.0))
        if v > 50:
            return RuleResult(
                triggered=True,
                rule_id="narrative_hot",
                reason=f"'{label}' velocity={v:.0f} mentions/h",
            )
    return RuleResult(
        triggered=False,
        rule_id="narrative_hot",
        reason="no matched narrative above 50 mentions/h",
    )


def rule_exit_dev_dump(ctx: dict[str, Any]) -> RuleResult:
    """Exit signal: dev cluster starts selling more than 20% of their position."""
    dev_sell_pct = float(ctx.get("dev_sell_pct") or 0.0)
    triggered = dev_sell_pct > 20
    return RuleResult(
        triggered=triggered,
        rule_id="exit_dev_dump",
        reason=f"dev sold {dev_sell_pct:.1f}% of position",
    )


def evaluate_rules(
    rules: list,
    ctx: dict[str, Any],
) -> list[RuleResult]:
    """Run a list of rule functions against a context dict.

    Catches individual rule exceptions and converts them to non-triggered results
    so one broken rule never silences the others.
    """
    results: list[RuleResult] = []
    for rule in rules:
        try:
            results.append(rule(ctx))
        except Exception as exc:
            results.append(
                RuleResult(
                    triggered=False,
                    rule_id=getattr(rule, "__name__", str(rule)),
                    reason=f"rule error: {exc}",
                )
            )
    return results


# ── Rule registries ────────────────────────────────────────────────────────────

ENTRY_RULES = [
    rule_smart_money_early,
    rule_low_bundle,
    rule_lp_burned,
    rule_narrative_hot,
]

EXIT_RULES = [
    rule_exit_dev_dump,
]
