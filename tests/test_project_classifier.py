"""Tests for project_classifier heuristics + meta extraction (offline, no LLM/network)."""

import pytest

from src.analyzer.project_classifier import (
    TokenMeta, heuristic_classify, _extract_meta_fields, _is_real_website,
)
from src.notifications.telegram import send_message


# ── _is_real_website ────────────────────────────────────────────────────────────

def test_real_website_true():
    assert _is_real_website("https://myproject.io") is True


def test_real_website_rejects_socials_and_launchpad():
    for u in ["https://t.me/foo", "https://x.com/foo", "https://pump.fun/abc", None, "notaurl"]:
        assert _is_real_website(u) is False


# ── heuristic_classify ──────────────────────────────────────────────────────────

def test_clear_project_website_plus_product_desc():
    m = TokenMeta(mint="M", name="DeFiVault", symbol="DVLT",
                  description="A yield aggregation protocol with a staking dashboard and governance.",
                  website="https://defivault.io")
    label, sig = heuristic_classify(m)
    assert label == "project"
    assert sig["has_website"] and sig["product_hits"] >= 1


def test_clear_meme_keywords_no_site():
    m = TokenMeta(mint="M", name="Pepe Doge Moon", symbol="PEPE", description="just a meme ser")
    assert heuristic_classify(m)[0] == "meme"


def test_bare_ticker_no_signal_is_meme():
    m = TokenMeta(mint="M", name="WIF", symbol="WIF", description="")
    assert heuristic_classify(m)[0] == "meme"


def test_ambiguous_website_only_no_product_words():
    # has a website but description is short and non-product, no meme words → ambiguous
    m = TokenMeta(mint="M", name="Zorp", symbol="ZRP", description="the future",
                  website="https://zorp.xyz")
    assert heuristic_classify(m)[0] == "ambiguous"


def test_strong_description_without_website_is_project_when_no_meme():
    m = TokenMeta(mint="M", name="ChainTool", symbol="CTL",
                  description=("A developer platform and SDK for building on-chain analytics "
                               "dashboards with a governance token and staking utility."))
    # no website but strong product description, no meme words → project
    assert heuristic_classify(m)[0] == "project"


# ── _extract_meta_fields ────────────────────────────────────────────────────────

def test_extract_meta_nested_token_and_socials():
    raw = {"token": {"name": "Foo", "symbol": "FOO", "description": "a platform",
                     "socials": {"twitter": "https://x.com/foo", "website": "https://foo.io"}}}
    m = _extract_meta_fields("M", raw)
    assert m.name == "Foo"
    assert m.website == "https://foo.io"
    assert m.twitter == "https://x.com/foo"


def test_extract_meta_empty():
    m = _extract_meta_fields("M", None)
    assert m.mint == "M"
    assert m.website is None


# ── telegram no-op when unconfigured ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_noop_without_config(monkeypatch):
    from src.common import config
    monkeypatch.setattr(config.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(config.settings, "telegram_chat_id", "", raising=False)
    assert await send_message("hi") is False
