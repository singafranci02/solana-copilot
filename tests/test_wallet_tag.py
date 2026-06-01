"""Tests for src/analyzer/wallet_tag.py — pure tag function (offline)."""

from src.analyzer.wallet_tag import MintTagContext, tag_wallet


def test_team_beats_everything():
    ctx = MintTagContext(team={"W1"}, known_rugger={"W1"}, smart_money={"W1"})
    assert tag_wallet(ctx, "W1") == "team"


def test_known_rugger_beats_smart_money():
    ctx = MintTagContext(known_rugger={"W1"}, smart_money={"W1"})
    assert tag_wallet(ctx, "W1") == "known_rugger"


def test_smart_money_when_not_team_or_rugger():
    ctx = MintTagContext(smart_money={"W1"}, grad_holders={"W1"})
    assert tag_wallet(ctx, "W1") == "smart_money"


def test_new_when_not_a_grad_holder():
    ctx = MintTagContext(grad_holders={"A", "B"})
    assert tag_wallet(ctx, "C") == "new"


def test_unknown_when_grad_holder_no_other_tag():
    ctx = MintTagContext(grad_holders={"A"})
    assert tag_wallet(ctx, "A") == "unknown"


def test_unknown_when_empty_context():
    ctx = MintTagContext()
    assert tag_wallet(ctx, "anyone") == "unknown"
