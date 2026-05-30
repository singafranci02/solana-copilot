"""Rules engine — launch-time rules and graduation-context structural reads.

Launch-time rules (ENTRY_RULES / EXIT_RULES):
  Pure functions, take a context dict, return RuleResult.
  Used by pump_monitor for the 60-second BC-phase analysis.

Graduation-context verdict (structural_read):
  Returns a StructuralRead with verdict SKIP / WATCH / STRUCTURALLY_SOUND.
  Hard SKIP conditions are checked first; remaining factors are scored.
  Used by graduation_monitor for post-graduation analysis.

Design principles:
  - All functions are pure: no IO, no side effects.
  - structural_read never auto-triggers warnings based on patterns below
    minimum sample size (is_significant=False from patterns.py).
"""

from dataclasses import dataclass
from typing import Any

from src.common.models import MemorySignals, StructuralRead


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


# ── Rule registries ───────────────────────────────────────────────────────────

ENTRY_RULES = [
    rule_smart_money_early,
    rule_low_bundle,
    rule_lp_burned,
    rule_narrative_hot,
]

EXIT_RULES = [
    rule_exit_dev_dump,
]


# ── Graduation-context structural verdict ─────────────────────────────────────

def structural_read(ctx: dict[str, Any]) -> StructuralRead:
    """Produce a StructuralRead verdict for a graduated token.

    Context keys (all optional — missing keys score 0):
      team_cluster      — TeamCluster | None
      funder_rep        — FunderReputation | None
      smart_money_count — int
      distribution_signal — str | None ("ACCUMULATING"/"HOLDING"/"DISTRIBUTING"/"DUMPED")
      bundle_pct        — float (supply_pct_at_graduation for team cluster)
      bc_top_holders    — list[dict]

    Verdict logic:
      SKIP             — any hard skip condition met
      STRUCTURALLY_SOUND — positive score >= 2 points with no negatives
      WATCH            — everything else (insufficient signal or mixed)
    """
    factors: list[str] = []
    score: int = 0
    team_cluster = ctx.get("team_cluster")
    funder_rep = ctx.get("funder_rep")
    sm_count = int(ctx.get("smart_money_count") or 0)
    dist_signal = ctx.get("distribution_signal")
    bundle_pct = float(ctx.get("bundle_pct") or 0.0)
    mem: MemorySignals | None = ctx.get("memory_signals")

    # Push graduation signals
    top_holder_pct    = float(ctx.get("top_holder_pct") or 0.0)
    top3_holder_pct   = float(ctx.get("top3_holder_pct") or 0.0)
    bc_duration_s     = int(ctx.get("bc_duration_seconds") or -1)
    unique_bc_buyers  = int(ctx.get("unique_bc_buyers") or 0)

    # ── Hard SKIP conditions ──────────────────────────────────────────────────

    if funder_rep and funder_rep.is_known_rugger:
        return StructuralRead(
            verdict="SKIP",
            confidence=0.90,
            dominant_factors=[
                f"known rugger: {funder_rep.rug_rate*100:.0f}% rug rate "
                f"across {len(funder_rep.graduated_mints)} launches"
            ],
            what_would_change="funder reputation improves with new clean launches",
            funder_is_known_rugger=True,
            smart_money_count=sm_count,
        )

    if dist_signal == "DUMPED":
        return StructuralRead(
            verdict="SKIP",
            confidence=0.95,
            dominant_factors=["token already DUMPED — liquidity gone"],
            what_would_change="n/a — irreversible",
            distribution_signal=dist_signal,
            smart_money_count=sm_count,
        )

    if team_cluster and team_cluster.supply_pct_at_graduation >= 50 and team_cluster.is_bc_sniper:
        return StructuralRead(
            verdict="SKIP",
            confidence=0.80,
            dominant_factors=[
                f"team holds {team_cluster.supply_pct_at_graduation:.1f}% "
                "as BC snipers — high distribution risk"
            ],
            what_would_change="team reduces position significantly before next check",
            bundle_pct=team_cluster.supply_pct_at_graduation,
            smart_money_count=sm_count,
        )

    # Hard SKIP: graduation push — single actor holds >50% and forced migration fast
    if top_holder_pct > 50 and (bc_duration_s < 300 or bc_duration_s == -1):
        return StructuralRead(
            verdict="SKIP",
            confidence=0.92,
            dominant_factors=[
                f"graduation push — top holder owns {top_holder_pct:.1f}% of supply, "
                f"BC completed in {bc_duration_s}s"
            ],
            what_would_change="n/a — bundled push graduation",
            smart_money_count=sm_count,
        )

    # Hard SKIP: top 3 holders own 75%+ — heavily bundled regardless of speed
    if top3_holder_pct > 75:
        return StructuralRead(
            verdict="SKIP",
            confidence=0.90,
            dominant_factors=[
                f"heavily bundled graduation — top 3 holders own {top3_holder_pct:.1f}% of supply"
            ],
            what_would_change="n/a — supply too concentrated at graduation",
            smart_money_count=sm_count,
        )

    # Memory: wallet graph hard SKIP — member appeared 2+ times in rug clusters
    if mem and mem.graph_hits:
        rug_hits = [h for h in mem.graph_hits if h.rug_co_appearances >= 2]
        if rug_hits:
            best = max(rug_hits, key=lambda h: h.rug_co_appearances)
            return StructuralRead(
                verdict="SKIP",
                confidence=0.85,
                dominant_factors=[
                    f"wallet {best.connected_wallet[:6]}.. co-appeared "
                    f"{best.rug_co_appearances}x in rug clusters with {best.known_wallet[:6]}.."
                ],
                what_would_change="wallet history clears over time with clean launches",
                smart_money_count=sm_count,
            )

    # ── Positive signals ──────────────────────────────────────────────────────

    if sm_count >= 2:
        score += 2
        factors.append(f"{sm_count} smart money wallets")
    elif sm_count == 1:
        score += 1
        factors.append("1 smart money wallet")

    if dist_signal == "HOLDING":
        score += 1
        factors.append("team cluster holding post-graduation")
    elif dist_signal == "ACCUMULATING":
        score += 2
        factors.append("team cluster accumulating post-graduation")

    if team_cluster and team_cluster.supply_pct_at_graduation < 20:
        score += 1
        factors.append(f"team supply pct low ({team_cluster.supply_pct_at_graduation:.1f}%) — low dump risk")

    if funder_rep and not funder_rep.is_known_rugger and len(funder_rep.graduated_mints) >= 8:
        if funder_rep.moon_rate >= 0.4:
            score += 1
            factors.append(
                f"funder has {funder_rep.moon_rate*100:.0f}% moon rate "
                f"({len(funder_rep.graduated_mints)} launches)"
            )

    # ── Negative signals ──────────────────────────────────────────────────────

    if dist_signal == "DISTRIBUTING":
        score -= 2
        factors.append("team cluster distributing — selling accelerating")

    if funder_rep and len(funder_rep.graduated_mints) >= 4 and funder_rep.rug_rate >= 0.5:
        score -= 1
        factors.append(
            f"funder partial rugger: {funder_rep.rug_rate*100:.0f}% rug rate "
            f"({len(funder_rep.graduated_mints)} launches, below significance threshold)"
        )

    # ── Graduation push scoring (soft signals below hard-SKIP thresholds) ────

    if top_holder_pct > 35:
        score -= 2
        factors.append(
            f"top holder owns {top_holder_pct:.1f}% at graduation — push risk"
        )
    elif top_holder_pct > 20:
        score -= 1
        factors.append(
            f"top holder owns {top_holder_pct:.1f}% at graduation — elevated concentration"
        )

    if top3_holder_pct > 60:
        score -= 1
        factors.append(
            f"top 3 holders own {top3_holder_pct:.1f}% — bundled graduation likely"
        )

    if 0 < bc_duration_s < 180:
        score -= 2
        factors.append(
            f"BC completed in {bc_duration_s}s — forced graduation push"
        )
    elif 0 < bc_duration_s < 600:
        score -= 1
        factors.append(
            f"BC completed in {bc_duration_s // 60}min — unusually fast graduation"
        )

    if unique_bc_buyers > 0 and unique_bc_buyers < 15:
        score -= 1
        factors.append(
            f"only {unique_bc_buyers} unique BC buyers — low organic participation"
        )

    # ── Memory signals ────────────────────────────────────────────────────────

    if mem:
        # Soft graph warning: co-appeared wallets but no confirmed rug link yet
        soft_graph_hits = [h for h in mem.graph_hits if h.co_appearances >= 2 and h.rug_co_appearances < 2]
        if soft_graph_hits:
            score -= 1
            best = max(soft_graph_hits, key=lambda h: h.co_appearances)
            factors.append(
                f"wallet {best.connected_wallet[:6]}.. previously seen with "
                f"{best.known_wallet[:6]}.. ({best.co_appearances}x, no confirmed rug yet)"
            )

        # Pump ring velocity
        if mem.launches_24h >= 3:
            score -= 1
            factors.append(
                f"funder launched {mem.launches_24h} tokens in 24h — pump ring signal"
            )
        elif mem.launches_7d >= 7:
            score -= 1
            factors.append(
                f"funder launched {mem.launches_7d} tokens in 7d — high velocity"
            )

        # Structural fingerprint match
        if mem.fingerprint_match:
            score -= 1
            fp = mem.fingerprint_match
            factors.append(
                f"structure matches known rug pattern "
                f"(distance={fp.distance:.2f}, funder {fp.funding_source[:6]}.. "
                f"{fp.rug_rate*100:.0f}% rug rate, n={fp.sample_count})"
            )

        # Distribution timing hint (informational only — no score impact)
        if mem.expected_dump_start_h is not None:
            factors.append(
                f"funder historically dumps at ~{mem.expected_dump_start_h:.1f}h "
                f"(n={mem.dump_start_count})"
            )

    # ── Verdict ───────────────────────────────────────────────────────────────

    if score >= 2 and dist_signal not in ("DISTRIBUTING", "DUMPED"):
        verdict = "STRUCTURALLY_SOUND"
        confidence = min(0.85, 0.5 + score * 0.1)
        what_would_change = "team starts distributing or smart money exits"
    elif score <= -1:
        verdict = "SKIP"
        confidence = min(0.85, 0.5 + abs(score) * 0.1)
        what_would_change = "distribution signal improves to HOLDING with no further selling"
    else:
        verdict = "WATCH"
        confidence = 0.50
        what_would_change = "smart money entry or confirmed holding pattern"

    return StructuralRead(
        verdict=verdict,
        confidence=round(confidence, 2),
        dominant_factors=factors or ["insufficient signal"],
        what_would_change=what_would_change,
        bundle_pct=bundle_pct,
        dev_pct=getattr(team_cluster, "supply_pct_at_graduation", 0.0) if team_cluster else 0.0,
        distribution_signal=dist_signal,
        funder_is_known_rugger=bool(funder_rep and funder_rep.is_known_rugger),
        smart_money_count=sm_count,
    )
