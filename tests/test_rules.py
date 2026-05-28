"""Tests for src/strategy/rules.py — pure functions, no IO."""

import pytest

from src.strategy.rules import (
    ENTRY_RULES,
    EXIT_RULES,
    evaluate_rules,
    rule_exit_dev_dump,
    rule_lp_burned,
    rule_low_bundle,
    rule_narrative_hot,
    rule_smart_money_early,
)


# ── rule_smart_money_early ────────────────────────────────────────────────────

def test_smart_money_early_triggers():
    ctx = {"smart_money_count": 3, "minutes_since_launch": 4.5}
    r = rule_smart_money_early(ctx)
    assert r.triggered is True
    assert r.rule_id == "smart_money_early"


def test_smart_money_early_exactly_2_wallets_at_5_min():
    ctx = {"smart_money_count": 2, "minutes_since_launch": 5.0}
    assert rule_smart_money_early(ctx).triggered is True


def test_smart_money_early_not_triggered_too_late():
    ctx = {"smart_money_count": 3, "minutes_since_launch": 6.0}
    assert rule_smart_money_early(ctx).triggered is False


def test_smart_money_early_not_triggered_too_few():
    ctx = {"smart_money_count": 1, "minutes_since_launch": 2.0}
    assert rule_smart_money_early(ctx).triggered is False


def test_smart_money_early_missing_keys():
    r = rule_smart_money_early({})
    assert r.triggered is False


# ── rule_low_bundle ───────────────────────────────────────────────────────────

def test_low_bundle_triggers_below_10():
    assert rule_low_bundle({"bundle_pct": 5.0}).triggered is True


def test_low_bundle_not_triggered_at_10():
    assert rule_low_bundle({"bundle_pct": 10.0}).triggered is False


def test_low_bundle_not_triggered_above_10():
    assert rule_low_bundle({"bundle_pct": 25.0}).triggered is False


def test_low_bundle_unknown_returns_false():
    r = rule_low_bundle({})
    assert r.triggered is False
    assert "unknown" in r.reason


# ── rule_lp_burned ────────────────────────────────────────────────────────────

def test_lp_burned_triggers():
    assert rule_lp_burned({"lp_burned": True}).triggered is True


def test_lp_burned_not_triggered():
    assert rule_lp_burned({"lp_burned": False}).triggered is False


def test_lp_burned_missing_key():
    assert rule_lp_burned({}).triggered is False


def test_lp_burned_rule_id():
    assert rule_lp_burned({"lp_burned": True}).rule_id == "lp_burned"


# ── rule_narrative_hot ────────────────────────────────────────────────────────

def test_narrative_hot_triggers():
    ctx = {
        "matched_narratives": ["ai", "doge"],
        "narrative_velocities": {"ai": 80.0, "doge": 20.0},
    }
    r = rule_narrative_hot(ctx)
    assert r.triggered is True
    assert "ai" in r.reason


def test_narrative_hot_not_triggered_below_threshold():
    ctx = {
        "matched_narratives": ["ai"],
        "narrative_velocities": {"ai": 49.9},
    }
    assert rule_narrative_hot(ctx).triggered is False


def test_narrative_hot_exactly_50_not_triggered():
    ctx = {"matched_narratives": ["ai"], "narrative_velocities": {"ai": 50.0}}
    assert rule_narrative_hot(ctx).triggered is False


def test_narrative_hot_no_matches():
    ctx = {"matched_narratives": [], "narrative_velocities": {}}
    assert rule_narrative_hot(ctx).triggered is False


def test_narrative_hot_missing_velocity():
    ctx = {"matched_narratives": ["ai"], "narrative_velocities": {}}
    assert rule_narrative_hot(ctx).triggered is False


# ── rule_exit_dev_dump ────────────────────────────────────────────────────────

def test_exit_dev_dump_triggers():
    assert rule_exit_dev_dump({"dev_sell_pct": 25.0}).triggered is True


def test_exit_dev_dump_not_triggered_at_20():
    assert rule_exit_dev_dump({"dev_sell_pct": 20.0}).triggered is False


def test_exit_dev_dump_missing_key():
    assert rule_exit_dev_dump({}).triggered is False


# ── evaluate_rules ────────────────────────────────────────────────────────────

def test_evaluate_rules_returns_one_per_rule():
    ctx = {"bundle_pct": 5.0, "lp_burned": True}
    results = evaluate_rules([rule_low_bundle, rule_lp_burned], ctx)
    assert len(results) == 2


def test_evaluate_rules_catches_exceptions():
    def bad_rule(ctx):
        raise RuntimeError("oops")

    results = evaluate_rules([bad_rule], {})
    assert len(results) == 1
    assert results[0].triggered is False
    assert "oops" in results[0].reason


def test_evaluate_rules_empty():
    assert evaluate_rules([], {}) == []


def test_evaluate_rules_multiple_triggered():
    ctx = {"bundle_pct": 5.0, "lp_burned": True}
    results = evaluate_rules([rule_low_bundle, rule_lp_burned], ctx)
    assert all(r.triggered for r in results)


# ── registries ────────────────────────────────────────────────────────────────

def test_entry_rules_non_empty():
    assert len(ENTRY_RULES) >= 4


def test_exit_rules_non_empty():
    assert len(EXIT_RULES) >= 1


def test_all_rules_are_callable():
    for rule in ENTRY_RULES + EXIT_RULES:
        assert callable(rule)
