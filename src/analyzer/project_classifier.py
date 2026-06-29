"""Classify a graduated token as a real PROJECT (website/app/product/utility) vs a MEME.

Strictness (per product decision): PROJECT requires a real website OR a substantive
product description. Obvious cases are decided by pure heuristics (no LLM); ambiguous
cases fall through to the local Ollama model.

Metadata source chain (fetch_token_meta): Solana Tracker token-info → DexScreener `info`.
(On-chain metadata-URI JSON is the authoritative description source and can be added as a
third tier if these prove insufficient — see plan.)
"""

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from src.common.config import settings

logger = logging.getLogger(__name__)

# Meme archetype tokens — strong meme signal in name/symbol/description
_MEME_WORDS = {
    "pepe", "frog", "doge", "shib", "inu", "cat", "kitty", "nyan", "wojak", "chad",
    "bobo", "moon", "elon", "trump", "maga", "wif", "bonk", "baby", "rocket", "pump",
    "meme", "coin", "lol", "wtf", "cum", "gay", "retard", "based", "giga", "florki",
}
# Product / utility vocabulary — signals a real project
_PRODUCT_WORDS = {
    "app", "platform", "protocol", "utility", "dapp", "dApp", "governance", "staking",
    "ecosystem", "infrastructure", "ai", "agent", "tool", "marketplace", "exchange",
    "wallet", "sdk", "api", "network", "launchpad", "analytics", "dashboard", "bot",
    "trading", "defi", "yield", "vault", "oracle", "bridge", "rollup", "node", "game",
    "engine", "framework", "terminal", "scanner", "tracker", "aggregator",
}
_STRONG_DESCRIPTION_LEN = 80   # chars — a real pitch, not a one-liner


@dataclass
class TokenMeta:
    mint: str
    name: str = ""
    symbol: str = ""
    description: str | None = None
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    image: str | None = None


@dataclass
class ClassificationResult:
    is_project: bool
    label: str          # "project" | "meme"
    confidence: float
    reason: str


# ── metadata fetch ───────────────────────────────────────────────────────────────

def _is_real_website(url: str | None) -> bool:
    """A website that isn't just a social/launchpad link."""
    if not url:
        return False
    u = url.lower()
    if not u.startswith("http"):
        return False
    junk = ("pump.fun", "t.me", "twitter.com", "x.com", "discord", "dexscreener", "birdeye")
    return not any(j in u for j in junk)


def _extract_meta_fields(mint: str, raw: dict | None) -> TokenMeta:
    """Pull meta fields from a Solana Tracker token-info response (shape-tolerant)."""
    if not raw:
        return TokenMeta(mint=mint)
    tok = raw.get("token") if isinstance(raw.get("token"), dict) else raw
    socials = tok.get("socials") if isinstance(tok.get("socials"), dict) else {}
    ext = tok.get("extensions") if isinstance(tok.get("extensions"), dict) else {}
    return TokenMeta(
        mint=mint,
        name=tok.get("name") or "",
        symbol=tok.get("symbol") or "",
        description=tok.get("description") or ext.get("description"),
        website=tok.get("website") or socials.get("website") or ext.get("website"),
        twitter=tok.get("twitter") or socials.get("twitter") or ext.get("twitter"),
        telegram=tok.get("telegram") or socials.get("telegram") or ext.get("telegram"),
        image=tok.get("image") or tok.get("logo"),
    )


async def _fetch_dexscreener_info(mint: str) -> dict:
    """DexScreener token info: websites[] + socials[] (no description). Fallback source."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
        pairs = data.get("pairs") or []
        for p in pairs:
            info = p.get("info") or {}
            if info:
                return info
    except Exception:
        pass
    return {}


async def fetch_token_meta(mint: str, st_client=None) -> TokenMeta:
    """Assemble token metadata from Solana Tracker, filling gaps from DexScreener."""
    meta = TokenMeta(mint=mint)
    if st_client is not None:
        try:
            meta = _extract_meta_fields(mint, await st_client.get_token_info(mint))
        except Exception as exc:
            logger.debug("ST token-info failed for %s: %s", mint[:8], exc)

    if not (meta.website or meta.twitter or meta.telegram):
        info = await _fetch_dexscreener_info(mint)
        websites = info.get("websites") or []
        if websites and not meta.website:
            meta.website = (websites[0] or {}).get("url")
        for soc in info.get("socials") or []:
            plat = (soc.get("platform") or soc.get("type") or "").lower()
            handle = soc.get("handle") or soc.get("url")
            if plat == "twitter" and not meta.twitter:
                meta.twitter = handle
            elif plat == "telegram" and not meta.telegram:
                meta.telegram = handle
    return meta


# ── classification ───────────────────────────────────────────────────────────────

def heuristic_classify(meta: TokenMeta) -> tuple[str, dict]:
    """Pure first-pass classifier → 'project' | 'meme' | 'ambiguous' + signals."""
    text = f"{meta.name} {meta.symbol} {meta.description or ''}".lower()
    desc = (meta.description or "").strip()

    has_website = _is_real_website(meta.website)
    strong_desc = len(desc) >= _STRONG_DESCRIPTION_LEN
    product_hits = sum(1 for w in _PRODUCT_WORDS if w in text)
    meme_hits = sum(1 for w in _MEME_WORDS if w in text)

    signals = {
        "has_website": has_website,
        "strong_desc": strong_desc,
        "product_hits": product_hits,
        "meme_hits": meme_hits,
        "desc_len": len(desc),
    }

    # Clear project: real website + product/desc framing, OR a strong product
    # description on its own (website-less projects), with no meme signal.
    if meme_hits == 0 and (
        (has_website and (product_hits >= 1 or strong_desc))
        or (strong_desc and product_hits >= 2)
    ):
        return "project", signals
    # Clear meme: meme words, no website, thin description
    if meme_hits >= 1 and not has_website and not strong_desc and product_hits == 0:
        return "meme", signals
    # No signal at all → meme (bare ticker, no site, no description)
    if not has_website and not strong_desc and product_hits == 0:
        return "meme", signals
    return "ambiguous", signals


async def classify_project(meta: TokenMeta) -> ClassificationResult:
    """Heuristic first; ambiguous cases go to the local LLM. Enforces the
    website-OR-strong-description bar for the final 'project' label."""
    label, signals = heuristic_classify(meta)

    if label == "project":
        return ClassificationResult(True, "project", 0.8,
                                    f"website+product signals {signals}")
    if label == "meme":
        return ClassificationResult(False, "meme", 0.8, f"meme/no-signal {signals}")

    # ambiguous → LLM
    verdict, reason = await _llm_classify(meta)
    gate = _is_real_website(meta.website) or len((meta.description or "").strip()) >= _STRONG_DESCRIPTION_LEN
    is_project = verdict == "project" and gate
    return ClassificationResult(
        is_project=is_project,
        label="project" if is_project else "meme",
        confidence=0.6,
        reason=reason or f"llm={verdict} gate={gate}",
    )


_LLM_PROMPT = """You classify Solana tokens as either a real PROJECT (has a genuine \
product: app, platform, protocol, tool, game, utility) or a MEME (joke/animal/celebrity \
/hype coin with no product). Answer with exactly one word: PROJECT or MEME, then a dash \
and a short reason.

Name: {name}
Symbol: {symbol}
Website: {website}
Description: {description}
"""


async def _llm_classify(meta: TokenMeta) -> tuple[str, str]:
    """Local Ollama classification. Returns (verdict, reason). Mirrors summarize._call_ollama."""
    prompt = _LLM_PROMPT.format(
        name=meta.name or "?", symbol=meta.symbol or "?",
        website=meta.website or "(none)", description=(meta.description or "(none)")[:500],
    )

    def _sync() -> str:
        import ollama as _ollama
        resp = _ollama.generate(model=settings.llm_model, prompt=prompt)
        return getattr(resp, "response", None) or resp.get("response", "") or ""

    try:
        out = (await asyncio.to_thread(_sync)).strip()
    except Exception as exc:
        logger.debug("LLM classify failed: %s", exc)
        return "meme", "llm-error"
    upper = out.upper()
    verdict = "project" if upper.startswith("PROJECT") else "meme"
    reason = out.split("-", 1)[1].strip() if "-" in out else out[:120]
    return verdict, reason
