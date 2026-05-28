"""Prompt templates for the LLM-based token analysis summarizer.

The prompt follows a structured RAW DATA → INSTRUCTIONS → EXAMPLE pattern so
the model has all context in one shot and knows the expected output format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.analyzer.summarize import TokenAnalysis

# ── persona ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a concise Solana memecoin trading analyst. "
    "Write trader-friendly summaries that cite specific numbers, identify the "
    "top risks and opportunities, and always end with a 'Read:' verdict line. "
    "Be direct and actionable. No fluff, no hedging."
)

# ── few-shot example ──────────────────────────────────────────────────────────

EXAMPLE_OUTPUT = (
    "Heads up on $PEPETOPIA. Dev cluster of 9 wallets funded from KuCoin "
    "11 minutes pre-launch bought 18% of supply via bundle. 3 smart money "
    "wallets (combined 73% win rate) bought at $45-52K MC and still holding. "
    "Twitter handle verified, 12K followers, active. "
    "Read: team is sketchy but smart money sees something — social narrative "
    "could run. If you enter, sub-2% position, exit if dev cluster starts "
    "moving tokens."
)

# ── known CEX / funder labels ─────────────────────────────────────────────────

_KNOWN_FUNDERS: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi": "Coinbase",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BrmHaL2HPFL":  "OKX",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7as5": "KuCoin",
    "HVh6wHNBAsG3pq1Bj5oCzRjoWKVogEDHwUHkRz3ekFgt": "Kraken",
}


def _funder_label(address: str) -> str:
    """Resolve a funding-source address to a human label."""
    if address in _KNOWN_FUNDERS:
        return _KNOWN_FUNDERS[address]
    if address == "cex":
        return "unknown CEX"
    return f"wallet …{address[-6:]}"


# ── renderer ──────────────────────────────────────────────────────────────────

def render_prompt(analysis: TokenAnalysis) -> str:
    """Build the full LLM user-message with analysis data injected.

    The rendered string is designed to be sent as the ``user`` turn; the
    ``SYSTEM_PROMPT`` is sent separately as the system message.
    """
    token = analysis.token
    cluster = analysis.team_cluster

    # ── token basics
    mc_str = (
        f"${token.market_cap_usd_snapshot:,.0f}"
        if token.market_cap_usd_snapshot is not None
        else "unknown"
    )
    bundle_pct = token.bundle_pct if token.bundle_pct is not None else analysis.raw_stats.get("bundle_pct")
    dev_pct = token.dev_pct if token.dev_pct is not None else analysis.raw_stats.get("dev_pct")
    bundle_str = f"{bundle_pct:.1f}% of supply" if bundle_pct is not None else "unknown"
    dev_str = f"{dev_pct:.1f}%" if dev_pct is not None else "unknown"

    # ── team cluster
    if cluster:
        funder = _funder_label(cluster.funding_source)
        launch_ts = analysis.token_launch_ts or token.created_at
        if launch_ts and cluster.funded_at:
            mins = (launch_ts - cluster.funded_at) // 60
            timing = f"{mins} min pre-launch"
        else:
            timing = "unknown time pre-launch"
        cluster_str = (
            f"{len(cluster.member_addresses)}-wallet cluster "
            f"funded from {funder} {timing}"
        )
    else:
        cluster_str = "No team cluster detected"

    # ── smart money
    sm = analysis.smart_money_entries
    if sm:
        avg_wr = sum(e.wallet.win_rate_90d or 0.0 for e in sm) / len(sm)
        entry_mcs = [e.entry_mc_usd for e in sm if e.entry_mc_usd is not None]
        if entry_mcs:
            mc_range = f"${min(entry_mcs) / 1_000:.0f}K–${max(entry_mcs) / 1_000:.0f}K MC"
        else:
            mc_range = "unknown MC"
        holding = "still holding" if all(e.is_holding for e in sm) else "some have exited"
        sm_str = (
            f"{len(sm)} wallet(s), avg {avg_wr * 100:.0f}% 90d win rate, "
            f"entered at {mc_range}, {holding}"
        )
    else:
        sm_str = "None"

    # ── past deployments
    dep_str = (
        f"{len(analysis.past_deployments)} previous token(s) by same dev"
        if analysis.past_deployments
        else "None found"
    )

    # ── social
    if analysis.social_handle:
        verified = "verified" if analysis.social_verified else "unverified"
        followers = (
            f"{analysis.social_followers:,}" if analysis.social_followers is not None else "unknown"
        )
        social_str = f"@{analysis.social_handle} ({verified}, {followers} followers)"
    else:
        social_str = "No Twitter data"

    # ── narratives
    narr_str = (
        ", ".join(analysis.matched_narratives) if analysis.matched_narratives else "None"
    )

    return (
        "## RAW DATA\n"
        f"Token: ${token.symbol} — {token.name} on {token.launchpad}\n"
        f"Market cap snapshot: {mc_str}\n"
        f"Bundle buy: {bundle_str}\n"
        f"Dev holding: {dev_str}\n"
        f"Past deployments by same dev: {dep_str}\n"
        "\n"
        f"Team cluster: {cluster_str}\n"
        f"Smart money: {sm_str}\n"
        f"Social: {social_str}\n"
        f"Narrative matches: {narr_str}\n"
        "\n"
        "## INSTRUCTIONS\n"
        "Write a single paragraph (3–5 sentences) in casual but precise trader language.\n"
        "- Cite the specific numbers from RAW DATA that matter most.\n"
        "- Name the 2–3 most important signals (bullish and bearish).\n"
        "- End with exactly one line starting with 'Read: ' containing your overall\n"
        "  verdict and a concrete suggested position or exit trigger.\n"
        "- Keep the whole response under 150 words.\n"
        "\n"
        "## EXAMPLE OUTPUT\n"
        f"{EXAMPLE_OUTPUT}\n"
    )
