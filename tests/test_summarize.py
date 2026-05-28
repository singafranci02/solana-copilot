"""Tests for src/analyzer/summarize.py and src/analyzer/prompts.py.

All LLM calls are mocked — no real Anthropic or Ollama traffic.
The three scenarios exercise distinct signal combinations:
  1. Rug-pull red flags   → low confidence, small position
  2. Mixed signals        → med confidence, moderate position
  3. Clean bullish token  → high confidence, full position
"""

import pytest
from unittest.mock import AsyncMock, patch

from src.analyzer.prompts import render_prompt
from src.analyzer.summarize import (
    AnalysisBundle,
    SmartMoneyEntry,
    SummaryMetadata,
    SummaryResult,
    TokenAnalysis,
    _compute_metadata,
    build_prompt,
    summarize,
)
from src.common.models import NarrativeState, Token, Wallet, WalletCluster

# ── shared timestamps ─────────────────────────────────────────────────────────

LAUNCH = 1_705_312_800  # 2024-01-15 10:00:00 UTC

# Coinbase hot wallet (known to prompts._KNOWN_FUNDERS)
COINBASE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi"
# Generic unknown funder
UNKNOWN_FUNDER = "UNKNOWNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

# ── scenario builders ─────────────────────────────────────────────────────────


def _rug_analysis() -> TokenAnalysis:
    """Scenario 1: all red flags, no bullish signals.

    9-wallet team cluster funded from Coinbase 11 min pre-launch,
    18% bundle, 42% dev holding, 2 previous deploys, no smart money, no social.
    """
    token = Token(
        mint="RUG1111111111111111111111111111111111111111",
        symbol="RUGCOIN",
        name="Rug Coin",
        launchpad="pump.fun",
        created_at=LAUNCH,
        market_cap_usd_snapshot=50_000.0,
        bundle_pct=18.0,
        dev_pct=42.0,
    )
    cluster = WalletCluster(
        cluster_id="cluster-rug-1",
        funding_source=COINBASE,
        funded_at=LAUNCH - 660,       # 11 min before launch
        funded_window_end=LAUNCH - 540,
        member_addresses=[f"wallet{i}" for i in range(9)],
        is_likely_team=True,
        total_sol_funded=45.0,
    )
    return TokenAnalysis(
        token=token,
        team_cluster=cluster,
        smart_money_entries=[],
        matched_narratives=[],
        narrative_states=[],
        past_deployments=["PREVTOKEN111111111111", "PREVTOKEN222222222222"],
        raw_stats={},
        token_launch_ts=LAUNCH,
        social_handle=None,
        social_followers=None,
        social_verified=None,
    )


def _mixed_analysis() -> TokenAnalysis:
    """Scenario 2: smart money present, small team cluster, narrative match.

    4-wallet team cluster funded 20 min pre-launch, 8% bundle,
    3 smart money wallets (65/72/83% win rate, $45-52K entry MC), still holding.
    """
    token = Token(
        mint="MIX1111111111111111111111111111111111111111",
        symbol="MIXCOIN",
        name="Mix Coin",
        launchpad="pump.fun",
        created_at=LAUNCH,
        market_cap_usd_snapshot=48_000.0,
        bundle_pct=8.0,
        dev_pct=12.0,
    )
    cluster = WalletCluster(
        cluster_id="cluster-mix-1",
        funding_source=UNKNOWN_FUNDER,
        funded_at=LAUNCH - 1_200,     # 20 min before launch
        funded_window_end=LAUNCH - 900,
        member_addresses=[f"teamwallet{i}" for i in range(4)],
        is_likely_team=True,
        total_sol_funded=20.0,
    )
    sm_wallets = [
        Wallet("sm1", win_rate_90d=0.65, smart_money_score=0.82),
        Wallet("sm2", win_rate_90d=0.72, smart_money_score=0.88),
        Wallet("sm3", win_rate_90d=0.83, smart_money_score=0.91),
    ]
    sm_entries = [
        SmartMoneyEntry(sm_wallets[0], entry_mc_usd=45_000.0, is_holding=True),
        SmartMoneyEntry(sm_wallets[1], entry_mc_usd=48_000.0, is_holding=True),
        SmartMoneyEntry(sm_wallets[2], entry_mc_usd=52_000.0, is_holding=True),
    ]
    return TokenAnalysis(
        token=token,
        team_cluster=cluster,
        smart_money_entries=sm_entries,
        matched_narratives=["pepe", "meme-season"],
        narrative_states=[
            NarrativeState(1, "pepe", ["pepe", "frog"], LAUNCH - 3600,
                           peak_velocity=45.0, current_velocity=32.0, status="hot"),
        ],
        past_deployments=[],
        raw_stats={},
        token_launch_ts=LAUNCH,
        social_handle=None,
        social_followers=None,
        social_verified=None,
    )


def _clean_analysis() -> TokenAnalysis:
    """Scenario 3: no team cluster, strong smart money, verified social, narrative.

    2 smart money wallets (85/80% win rate, $40-45K entry), still holding.
    Verified Twitter @pepetopia with 12K followers, narrative 'ai-agents'.
    """
    token = Token(
        mint="CLEAN1111111111111111111111111111111111111",
        symbol="PEPETOPIA",
        name="Pepetopia",
        launchpad="pump.fun",
        created_at=LAUNCH,
        market_cap_usd_snapshot=45_000.0,
        bundle_pct=2.0,
        dev_pct=3.0,
    )
    sm_wallets = [
        Wallet("sm_a", win_rate_90d=0.85, smart_money_score=0.93),
        Wallet("sm_b", win_rate_90d=0.80, smart_money_score=0.87),
    ]
    sm_entries = [
        SmartMoneyEntry(sm_wallets[0], entry_mc_usd=40_000.0, is_holding=True),
        SmartMoneyEntry(sm_wallets[1], entry_mc_usd=45_000.0, is_holding=True),
    ]
    return TokenAnalysis(
        token=token,
        team_cluster=None,
        smart_money_entries=sm_entries,
        matched_narratives=["ai-agents"],
        narrative_states=[
            NarrativeState(2, "ai-agents", ["ai", "agent"], LAUNCH - 7200,
                           peak_velocity=80.0, current_velocity=65.0, status="hot"),
        ],
        past_deployments=[],
        raw_stats={},
        token_launch_ts=LAUNCH,
        social_handle="pepetopia",
        social_followers=12_000,
        social_verified=True,
    )


# ── pre-crafted LLM responses ─────────────────────────────────────────────────

_RUG_SUMMARY = (
    "$RUGCOIN is a textbook rug setup. Dev cluster of 9 wallets funded from "
    "Coinbase 11 min pre-launch snagged 18% of supply via bundle and still holds 42%. "
    "Zero smart money presence and 2 prior deploys by the same dev — both ended badly. "
    "Read: avoid entirely; if you're gambling, sub-1% max, exit the moment dev "
    "wallets move tokens."
)

_MIXED_SUMMARY = (
    "$MIXCOIN has real tension. A 4-wallet team cluster scooped up 8% pre-launch "
    "from an unknown funder 20 min before trade opened — not ideal, but not a nuclear "
    "red flag. More interesting: 3 smart money wallets averaging 73% win rate entered "
    "at $45-52K MC and are still holding. Pepe/meme-season narrative is hot at "
    "32 mentions/hr. "
    "Read: smart money wins this debate; 1.5-2% position, hard exit if team cluster "
    "starts distributing."
)

_CLEAN_SUMMARY = (
    "$PEPETOPIA looks clean. No team cluster detected, only 2% bundle. Two elite "
    "smart money wallets (avg 82% win rate) entered at $40-45K MC and are still in. "
    "Verified @pepetopia with 12,000 followers and ai-agents narrative running hot "
    "at 65 mentions/hr. "
    "Read: best setup in the scanner today — 3-4% position, trail stop below $40K MC."
)


# ── scenario 1: rug-pull (all red flags) ──────────────────────────────────────

class TestRugPullScenario:
    def setup_method(self):
        self.analysis = _rug_analysis()

    def test_prompt_contains_cluster_size(self):
        prompt = render_prompt(self.analysis)
        assert "9-wallet cluster" in prompt

    def test_prompt_contains_bundle_pct(self):
        prompt = render_prompt(self.analysis)
        assert "18.0%" in prompt

    def test_prompt_contains_funder_label(self):
        # Coinbase address should resolve to the human label
        prompt = render_prompt(self.analysis)
        assert "Coinbase" in prompt

    def test_prompt_contains_timing(self):
        # 660 seconds before launch = 11 min
        prompt = render_prompt(self.analysis)
        assert "11 min pre-launch" in prompt

    def test_prompt_contains_past_deployments(self):
        prompt = render_prompt(self.analysis)
        assert "2 previous token(s)" in prompt

    def test_metadata_signals_all_red(self):
        meta = _compute_metadata(self.analysis)
        assert "team_cluster" in meta.top_signals
        assert "high_bundle" in meta.top_signals
        assert "serial_dev" in meta.top_signals
        assert "smart_money" not in meta.top_signals

    def test_metadata_confidence_low(self):
        meta = _compute_metadata(self.analysis)
        assert meta.confidence == "low"

    def test_metadata_position_small(self):
        meta = _compute_metadata(self.analysis)
        # 3.0 - 3*1.0 = 0.0 → clamped to 0.5
        assert meta.suggested_position_pct == pytest.approx(0.5)

    async def test_summarize_returns_summary_result(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_RUG_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert isinstance(result, SummaryResult)
        assert result.text == _RUG_SUMMARY
        assert result.metadata.confidence == "low"

    async def test_summarize_rug_text_mentions_key_facts(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_RUG_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert "9" in result.text        # cluster size
        assert "18%" in result.text      # bundle pct
        assert "Coinbase" in result.text # funder label
        assert "Read:" in result.text    # verdict line present


# ── scenario 2: mixed signals ─────────────────────────────────────────────────

class TestMixedSignalsScenario:
    def setup_method(self):
        self.analysis = _mixed_analysis()

    def test_prompt_contains_smart_money_count(self):
        prompt = render_prompt(self.analysis)
        assert "3 wallet(s)" in prompt

    def test_prompt_contains_avg_win_rate(self):
        # avg of 0.65+0.72+0.83 = 0.7333... → rendered as 73%
        prompt = render_prompt(self.analysis)
        assert "73%" in prompt

    def test_prompt_contains_entry_mc_range(self):
        prompt = render_prompt(self.analysis)
        assert "$45K" in prompt
        assert "$52K" in prompt

    def test_prompt_contains_narrative(self):
        prompt = render_prompt(self.analysis)
        assert "pepe" in prompt.lower()

    def test_prompt_contains_cluster_timing(self):
        # 1200 seconds = 20 min
        prompt = render_prompt(self.analysis)
        assert "20 min pre-launch" in prompt

    def test_metadata_has_both_signals(self):
        meta = _compute_metadata(self.analysis)
        assert "team_cluster" in meta.top_signals
        assert "smart_money" in meta.top_signals
        assert "narrative" in meta.top_signals

    def test_metadata_confidence_med(self):
        meta = _compute_metadata(self.analysis)
        assert meta.confidence == "med"

    def test_metadata_position_moderate(self):
        # 3.0 + 2*0.5 - 1*1.0 = 3.0 (narrative + smart_money green; team_cluster red)
        meta = _compute_metadata(self.analysis)
        assert 1.0 <= meta.suggested_position_pct <= 4.0

    async def test_summarize_mixed_text_mentions_key_facts(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_MIXED_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert "4" in result.text        # cluster size
        assert "73%" in result.text      # avg win rate
        assert "$45" in result.text      # entry MC floor
        assert "Read:" in result.text

    async def test_summarize_metadata_attached(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_MIXED_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert result.metadata.confidence == "med"
        assert "smart_money" in result.metadata.top_signals


# ── scenario 3: clean bullish token ───────────────────────────────────────────

class TestCleanBullishScenario:
    def setup_method(self):
        self.analysis = _clean_analysis()

    def test_prompt_no_team_cluster(self):
        prompt = render_prompt(self.analysis)
        assert "No team cluster detected" in prompt

    def test_prompt_contains_social(self):
        prompt = render_prompt(self.analysis)
        assert "@pepetopia" in prompt
        assert "verified" in prompt
        assert "12,000" in prompt

    def test_prompt_contains_narrative(self):
        prompt = render_prompt(self.analysis)
        assert "ai-agents" in prompt

    def test_prompt_contains_sm_win_rate(self):
        # avg(0.85, 0.80) = 0.825 → rendered as 82%
        prompt = render_prompt(self.analysis)
        assert "82%" in prompt

    def test_prompt_mc_range(self):
        prompt = render_prompt(self.analysis)
        assert "$40K" in prompt
        assert "$45K" in prompt

    def test_metadata_all_green_signals(self):
        meta = _compute_metadata(self.analysis)
        assert "smart_money" in meta.top_signals
        assert "narrative" in meta.top_signals
        assert "verified_social" in meta.top_signals
        assert "team_cluster" not in meta.top_signals

    def test_metadata_confidence_high(self):
        meta = _compute_metadata(self.analysis)
        assert meta.confidence == "high"

    def test_metadata_position_full(self):
        # 3.0 + 3*0.5 - 0*1.0 = 4.5
        meta = _compute_metadata(self.analysis)
        assert meta.suggested_position_pct == pytest.approx(4.5)

    async def test_summarize_clean_text_mentions_key_facts(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_CLEAN_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert "$PEPETOPIA" in result.text or "PEPETOPIA" in result.text
        assert "82%" in result.text       # avg win rate
        assert "12,000" in result.text    # follower count
        assert "Read:" in result.text

    async def test_summarize_high_confidence_metadata(self):
        with patch(
            "src.analyzer.summarize._call_anthropic",
            new_callable=AsyncMock,
            return_value=_CLEAN_SUMMARY,
        ):
            result = await summarize(self.analysis, provider="anthropic")

        assert result.metadata.confidence == "high"
        assert result.metadata.suggested_position_pct >= 3.0


# ── cross-cutting: API surface ─────────────────────────────────────────────────

def test_analysis_bundle_alias():
    """AnalysisBundle must remain a valid alias for TokenAnalysis."""
    assert AnalysisBundle is TokenAnalysis


def test_build_prompt_delegates_to_render():
    analysis = _clean_analysis()
    assert build_prompt(analysis) == render_prompt(analysis)


async def test_summarize_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        await summarize(_clean_analysis(), provider="openai")


async def test_summarize_ollama_routes_to_ollama_fn():
    with patch(
        "src.analyzer.summarize._call_ollama",
        new_callable=AsyncMock,
        return_value="Ollama says hi. Read: test passed.",
    ) as mock_ollama:
        result = await summarize(_clean_analysis(), provider="ollama")

    mock_ollama.assert_called_once()
    assert "Ollama says hi" in result.text


async def test_summarize_anthropic_routes_to_anthropic_fn():
    with patch(
        "src.analyzer.summarize._call_anthropic",
        new_callable=AsyncMock,
        return_value="Claude says hi. Read: test passed.",
    ) as mock_claude:
        result = await summarize(_clean_analysis(), provider="anthropic")

    mock_claude.assert_called_once()
    assert "Claude says hi" in result.text


def test_smart_money_wallets_property():
    analysis = _mixed_analysis()
    wallets = analysis.smart_money_wallets
    assert len(wallets) == 3
    assert all(isinstance(w, Wallet) for w in wallets)


def test_summary_metadata_fields():
    meta = SummaryMetadata(
        top_signals=["team_cluster", "smart_money"],
        confidence="med",
        suggested_position_pct=2.0,
    )
    assert meta.confidence == "med"
    assert len(meta.top_signals) == 2
    assert meta.suggested_position_pct == pytest.approx(2.0)
