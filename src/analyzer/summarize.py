"""Generate a plain-English trader-friend summary via Ollama or Claude.

Entry point:  summarize(analysis, provider="ollama") -> SummaryResult
Legacy entry: generate_summary(bundle) -> str   (kept for backward compat)
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.analyzer.prompts import SYSTEM_PROMPT, render_prompt
from src.common.config import settings
from src.common.models import NarrativeState, Token, Wallet, WalletCluster


# ── data types ─────────────────────────────────────────────────────────────────

@dataclass
class SmartMoneyEntry:
    """One smart-money wallet's position in a specific token."""

    wallet: Wallet
    entry_mc_usd: float | None = None   # market cap at the time of entry
    is_holding: bool = True             # False once they exit


@dataclass
class SummaryMetadata:
    """Deterministic metadata derived from the analysis before the LLM call."""

    top_signals: list[str]          # e.g. ["team_cluster", "smart_money"]
    confidence: str                 # "low" | "med" | "high"
    suggested_position_pct: float   # e.g. 1.5


@dataclass
class SummaryResult:
    """Full output of summarize(): plain-English text plus structured metadata."""

    text: str
    metadata: SummaryMetadata


@dataclass
class TokenAnalysis:
    """All structured signals assembled before the LLM call."""

    token: Token
    team_cluster: WalletCluster | None
    smart_money_entries: list[SmartMoneyEntry]
    matched_narratives: list[str]
    narrative_states: list[NarrativeState]
    past_deployments: list[str]          # mint addresses by same dev
    raw_stats: dict[str, Any]            # bundle_pct, dev_pct, top10_pct, …
    token_launch_ts: int | None = None   # override for token.created_at
    social_handle: str | None = None
    social_followers: int | None = None
    social_verified: bool | None = None

    @property
    def smart_money_wallets(self) -> list[Wallet]:
        """Backward-compat: flat list of Wallet objects from smart_money_entries."""
        return [e.wallet for e in self.smart_money_entries]


# Keep the old name so existing importers don't break.
AnalysisBundle = TokenAnalysis


# ── deterministic metadata ────────────────────────────────────────────────────

def _compute_metadata(analysis: TokenAnalysis) -> SummaryMetadata:
    """Derive SummaryMetadata from the analysis without any LLM call."""
    signals: list[str] = []
    red = 0
    green = 0

    # Red-flag signals
    if analysis.team_cluster and len(analysis.team_cluster.member_addresses) >= 3:
        signals.append("team_cluster")
        red += 1

    bundle_pct = (
        analysis.token.bundle_pct
        if analysis.token.bundle_pct is not None
        else analysis.raw_stats.get("bundle_pct", 0.0)
    )
    if bundle_pct and bundle_pct > 15:
        signals.append("high_bundle")
        red += 1

    if analysis.past_deployments:
        signals.append("serial_dev")
        red += 1

    # Bullish signals
    if len(analysis.smart_money_entries) >= 2:
        signals.append("smart_money")
        green += 1

    if analysis.matched_narratives:
        signals.append("narrative")
        green += 1

    if analysis.social_verified and (analysis.social_followers or 0) >= 5_000:
        signals.append("verified_social")
        green += 1

    # Confidence: "high" only when there are no red flags at all
    if red == 0 and green >= 2:
        confidence = "high"
    elif red >= 2 and green == 0:
        confidence = "low"
    elif green == 0 and red == 0:
        confidence = "low"
    else:
        confidence = "med"

    # Suggested position: base 3%, +0.5 per green signal, -1.0 per red signal
    position_pct = 3.0 + green * 0.5 - red * 1.0
    position_pct = round(max(0.5, min(5.0, position_pct)), 1)

    return SummaryMetadata(
        top_signals=signals,
        confidence=confidence,
        suggested_position_pct=position_pct,
    )


# ── LLM backends ──────────────────────────────────────────────────────────────

async def _call_anthropic(prompt: str) -> str:
    """Call Claude via the Anthropic async SDK with prompt caching on the system turn."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


async def _call_ollama(prompt: str) -> str:
    """Call a local Ollama model in a thread (ollama SDK is synchronous)."""
    import ollama as _ollama

    def _sync() -> str:
        resp = _ollama.generate(
            model=settings.llm_model,
            prompt=f"{SYSTEM_PROMPT}\n\n{prompt}",
        )
        # ollama >=0.4 returns a GenerateResponse with .response
        return getattr(resp, "response", None) or resp.get("response", "")

    return await asyncio.to_thread(_sync)


# ── primary entry point ───────────────────────────────────────────────────────

async def summarize(
    analysis: TokenAnalysis,
    provider: str = "ollama",
) -> SummaryResult:
    """Generate a plain-English summary and structured metadata for a token.

    Args:
        analysis: All assembled signals for the token.
        provider: "ollama" (local Llama 3.1 8B) or "anthropic" (Claude).

    Returns:
        SummaryResult with .text and .metadata.
    """
    metadata = _compute_metadata(analysis)
    prompt = render_prompt(analysis)

    if provider == "anthropic":
        text = await _call_anthropic(prompt)
    elif provider == "ollama":
        text = await _call_ollama(prompt)
    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}. Use 'ollama' or 'anthropic'.")

    return SummaryResult(text=text, metadata=metadata)


# ── legacy entry points (smoke-test & backward compat) ────────────────────────

def build_prompt(bundle: TokenAnalysis) -> str:
    """Render the LLM prompt from a TokenAnalysis / AnalysisBundle.

    Args:
        bundle: Assembled analysis data.

    Returns:
        Formatted prompt string ready for the LLM.
    """
    return render_prompt(bundle)


async def generate_summary(bundle: TokenAnalysis) -> str:
    """Call the configured LLM and return the plain-English summary text.

    Routes to Ollama or Anthropic based on settings.llm_provider.

    Args:
        bundle: Assembled analysis data.

    Returns:
        Plain-English trader summary as a string.
    """
    result = await summarize(bundle, provider=settings.llm_provider)
    return result.text
