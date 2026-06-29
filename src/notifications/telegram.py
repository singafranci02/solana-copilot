"""Telegram alerts — fire-and-forget, no library needed (plain HTTPS POST).

Disabled (silent no-op) unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
Mirrors the supabase_sync resilience pattern: never raises into the caller.

Setup: create a bot via @BotFather (get the token); message the bot, then get your
chat_id from @userinfobot. Put both in .env.
"""

import html
import logging

import aiohttp

from src.common.config import settings

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True on success, False on no-op/failure."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                f"{_API}/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "link_preview_options": {"is_disabled": True},
                },
            ) as resp:
                if resp.status != 200:
                    logger.debug("telegram send failed: %s %s", resp.status, await resp.text())
                    return False
                return True
    except Exception as exc:
        logger.debug("telegram send error: %s", exc)
        return False


def _esc(s: str | None) -> str:
    return html.escape(s) if s else ""


async def notify_project_graduation(
    *,
    symbol: str,
    name: str,
    mint: str,
    description: str | None,
    website: str | None,
    twitter: str | None,
    telegram: str | None,
    verdict: str,
    confidence: float,
    bundled_supply_pct: float | None = None,
    largest_entity_supply_pct: float | None = None,
) -> None:
    """Format + send a 'project graduated' alert. Fire-and-forget."""
    lines = [f"🚀 <b>PROJECT GRADUATED — ${_esc(symbol)}</b>"]
    if name and name != symbol:
        lines.append(_esc(name))
    if description:
        snippet = description.strip()
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        lines.append(f"\n{_esc(snippet)}")

    lines.append(f"\n<b>Verdict:</b> {_esc(verdict)} ({confidence*100:.0f}%)")
    flags = []
    if bundled_supply_pct is not None:
        flags.append(f"bundled {bundled_supply_pct:.0f}%")
    if largest_entity_supply_pct is not None and largest_entity_supply_pct > 0:
        flags.append(f"top entity {largest_entity_supply_pct:.0f}%")
    if flags:
        lines.append("⚠ " + " · ".join(flags))

    links = []
    if website:
        links.append(f'<a href="{_esc(website)}">website</a>')
    if twitter:
        links.append(f'<a href="{_esc(twitter)}">twitter</a>')
    if telegram:
        links.append(f'<a href="{_esc(telegram)}">telegram</a>')
    links.append(f'<a href="https://pump.fun/{mint}">pump.fun</a>')
    links.append(f'<a href="https://dexscreener.com/solana/{mint}">dexscreener</a>')
    links.append(f'<a href="https://solscan.io/token/{mint}">solscan</a>')
    lines.append("\n" + " · ".join(links))

    lines.append(f"\n<code>{_esc(mint)}</code>")

    await send_message("\n".join(lines))
