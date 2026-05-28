"""Smoke tests — verify every module imports without error.

These tests do not call any real APIs or write to disk.
They only confirm the module graph is intact and type annotations are valid.
"""

import pytest


def test_common_config():
    from src.common import config
    assert hasattr(config, "settings")
    assert config.settings.llm_provider in ("ollama", "anthropic")
    assert config.settings.server_port == 8000


def test_common_db():
    from src.common import db
    assert callable(db.get_connection)
    assert callable(db.migrate)


def test_common_models():
    from src.common.models import (
        DistributionSignal,
        FunderReputation,
        GraduationEvent,
        NarrativeMention,
        NarrativeState,
        PostGradBehavior,
        StructuralRead,
        TeamCluster,
        Token,
        TokenBuyer,
        Trade,
        Wallet,
        WalletCluster,
        WalletStats,
    )
    w = Wallet(address="ABC")
    assert w.address == "ABC"
    assert w.smart_money_score == 0.0

    t = Token(mint="M", symbol="FOO", name="Foobar", launchpad="pump.fun", created_at=0)
    assert t.launchpad == "pump.fun"
    assert t.narrative_tags == []

    trade = Trade(
        tx_signature="SIG",
        token_mint="M",
        side="buy",
        ts=0,
        sol_amount=1.0,
        tokens=1000.0,
        price_sol=0.001,
    )
    assert trade.side == "buy"

    # Graduation-context models
    ge = GraduationEvent(token_mint="M", graduated_at=0)
    assert ge.bc_top_holders == []

    tc = TeamCluster(cluster_id="uuid", token_mint="M")
    assert tc.is_bc_sniper is False

    assert DistributionSignal.DUMPED.value == "DUMPED"
    assert DistributionSignal.HOLDING.value == "HOLDING"


def test_ingest_helius():
    from src.ingest import helius
    assert hasattr(helius, "HeliusClient")
    assert callable(helius.decode_swap_transaction)
    assert callable(helius.extract_funding_source)


def test_ingest_gmgn():
    from src.ingest import gmgn
    assert hasattr(gmgn, "GMGNClient")
    assert callable(gmgn.parse_wallet_profile)
    assert callable(gmgn.parse_token_info)


def test_ingest_bags():
    from src.ingest import bags
    assert hasattr(bags, "BagsClient")
    assert callable(bags.parse_bags_token)
    assert callable(bags.parse_bags_trade)


def test_ingest_x_ingest():
    from src.ingest import x_ingest
    assert hasattr(x_ingest, "XClient")
    assert callable(x_ingest.is_x_configured)


def test_analyzer_wallet_cluster():
    from src.analyzer import wallet_cluster
    assert callable(wallet_cluster.cluster_buyers)
    assert callable(wallet_cluster.compute_bundle_pct)


def test_analyzer_team_detect():
    from src.analyzer import team_detect
    assert callable(team_detect.identify_team_cluster)
    assert callable(team_detect.compute_dev_pct)
    assert callable(team_detect.get_past_deployments)
    assert callable(team_detect.build_team_cluster_post_grad)


def test_analyzer_smart_money():
    from src.analyzer import smart_money
    assert callable(smart_money.get_smart_money_wallets)
    assert callable(smart_money.score_wallet)
    assert callable(smart_money.find_smart_money_in_buyers)


def test_analyzer_narrative_match():
    from src.analyzer import narrative_match
    assert callable(narrative_match.get_active_narratives)
    assert callable(narrative_match.match_token_to_narratives)
    assert callable(narrative_match.narrative_velocity_at_entry)


def test_analyzer_summarize():
    from src.analyzer import summarize
    assert hasattr(summarize, "AnalysisBundle")
    assert callable(summarize.build_prompt)
    assert callable(summarize.generate_summary)


def test_strategy_rules():
    from src.strategy import rules
    assert callable(rules.evaluate_rules)
    assert callable(rules.structural_read)
    assert isinstance(rules.ENTRY_RULES, list)
    assert isinstance(rules.EXIT_RULES, list)
    assert len(rules.ENTRY_RULES) > 0


def test_common_cex_wallets():
    from src.common.cex_wallets import get_all_cex_addresses, is_cex_wallet, seed_cex_table
    assert callable(is_cex_wallet)
    assert callable(seed_cex_table)
    assert callable(get_all_cex_addresses)
    # is_cex_wallet works without DB
    assert is_cex_wallet("H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS") is True
    assert is_cex_wallet("nonexistent_wallet_address") is False


def test_analyzer_distribution():
    from src.analyzer import distribution
    assert callable(distribution.schedule_distribution_checks)
    assert callable(distribution.get_latest_signal)
    assert callable(distribution._classify)


def test_analyzer_patterns():
    from src.analyzer import patterns
    assert callable(patterns.rug_rate_by_team_supply_pct)
    assert callable(patterns.rug_rate_by_sniper_flag)
    assert callable(patterns.moon_rate_by_smart_money_count)
    assert callable(patterns.distribution_signal_vs_outcome)
    assert callable(patterns.all_patterns)
    assert patterns.MIN_SIGNIFICANT_N == 30


def test_ingest_graduation_monitor():
    from src.ingest import graduation_monitor
    assert hasattr(graduation_monitor, "GraduationMonitor")
    assert callable(graduation_monitor.monitor)
    assert callable(graduation_monitor.analyse_graduation)


def test_smart_money_new_functions():
    from src.analyzer.smart_money import (
        get_funder_reputation,
        get_wallet_stats,
        update_funder_reputation,
        update_wallet_stats,
    )
    assert callable(update_wallet_stats)
    assert callable(get_wallet_stats)
    assert callable(update_funder_reputation)
    assert callable(get_funder_reputation)


def test_strategy_backtest():
    from src.strategy import backtest
    assert hasattr(backtest, "BacktestResult")
    assert callable(backtest.run_backtest)
    assert callable(backtest.print_backtest_report)


def test_services_analyzer_server():
    from src.services import analyzer_server
    assert hasattr(analyzer_server, "app")


def test_services_wallet_watcher():
    from src.services import wallet_watcher
    assert callable(wallet_watcher.watch_wallets)
    assert callable(wallet_watcher.poll_wallet)


def test_services_narrative_tracker():
    from src.services import narrative_tracker
    assert callable(narrative_tracker.track_narratives)
    assert callable(narrative_tracker.compute_velocity)
    assert callable(narrative_tracker.classify_status)


def test_services_journal():
    from src.services import journal
    assert callable(journal.ingest_tx)
    assert callable(journal.save_trade)
    assert callable(journal.update_trade_tags)
    assert callable(journal.compute_trade_pnl)
