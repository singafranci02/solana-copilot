"""Point-in-time social state at graduation — free sources only (Phase 5, partial).

NON-RECOVERABLE: a project's Telegram member count, whether its website is live,
and how old its domain is AT GRADUATION cannot be re-queried later. Captured with
zero paid API:
  - Telegram members: scrape the public t.me preview (no bot, no credentials).
  - Website liveness: a plain HTTP GET.
  - Domain age: best-effort WHOIS creation date over a raw socket.
Twitter follower counts need a paid API and are deliberately skipped for now —
only Twitter *presence* is recorded here.

Runs fire-and-forget after the verdict; results land in graduation_social and are
joined by the training exporter (not in the leak-proof snapshot, which is written
synchronously before this network work completes).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; solana-copilot/1.0)"}
_TG_COUNT = re.compile(r"([\d  , ]+)\s+(subscriber|member)", re.I)
_WHOIS_CREATED = re.compile(
    r"(?:creation date|created|registered on|registration time)\s*:?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]\d{4})", re.I)


# ── pure parsers (unit-testable, no network) ──────────────────────────────────

def extract_tg_username(url: str | None) -> str | None:
    """Public channel/group username from a telegram URL or @handle."""
    if not url:
        return None
    u = url.strip().lstrip("@")
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{3,})", u)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_]{3,}", u):
        return u
    return None


def parse_tg_members(html: str) -> int | None:
    """Member/subscriber count from a t.me public-preview page."""
    m = _TG_COUNT.search(html or "")
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    return int(digits) if digits else None


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    if not url.startswith("http"):
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def parse_whois_creation(text: str) -> int | None:
    """Domain-age in days from a WHOIS response's creation date, or None."""
    m = _WHOIS_CREATED.search(text or "")
    if not m:
        return None
    raw = m.group(1)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            created = time.mktime(time.strptime(raw, fmt))
            return max(0, int((time.time() - created) / 86400))
        except ValueError:
            continue
    return None


# ── IO ────────────────────────────────────────────────────────────────────────

async def _fetch_telegram_members(session, username: str) -> int | None:
    try:
        async with session.get(f"https://t.me/{username}",
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            return parse_tg_members(await r.text())
    except Exception:
        return None


async def _fetch_website(session, url: str) -> tuple[bool, int | None, str | None]:
    """(is_live, http_status, final_url)."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               allow_redirects=True, ssl=False) as r:
            return (r.status < 500, r.status, str(r.url))
    except Exception:
        return (False, None, None)


def _whois_domain_age_days(domain: str) -> int | None:
    """Best-effort raw-socket WHOIS creation-date lookup (sync; run in a thread)."""
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    server = f"{tld}.whois-servers.net"
    try:
        with socket.create_connection((server, 43), timeout=6) as s:
            s.sendall((domain + "\r\n").encode())
            chunks = []
            while len(b"".join(chunks)) < 20000:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
        return parse_whois_creation(b"".join(chunks).decode("utf-8", "ignore"))
    except Exception:
        return None


async def capture_social(meta) -> dict:
    """Gather free point-in-time social signals for a token's metadata."""
    tg_user = extract_tg_username(getattr(meta, "telegram", None))
    domain = extract_domain(getattr(meta, "website", None))
    out = {
        "has_twitter": bool(getattr(meta, "twitter", None)),
        "has_telegram": bool(getattr(meta, "telegram", None)),
        "has_website": bool(domain),
        "tg_members": None, "website_live": None, "website_status": None,
        "website_final_url": None, "website_domain_age_days": None,
        "captured_at": int(time.time()),
    }
    async with aiohttp.ClientSession(headers=_UA) as session:
        tasks = {}
        if tg_user:
            tasks["tg"] = asyncio.create_task(_fetch_telegram_members(session, tg_user))
        if getattr(meta, "website", None):
            tasks["web"] = asyncio.create_task(_fetch_website(session, meta.website))
        if "tg" in tasks:
            out["tg_members"] = await tasks["tg"]
        if "web" in tasks:
            live, status, final = await tasks["web"]
            out["website_live"] = live
            out["website_status"] = status
            out["website_final_url"] = final
    if domain:
        out["website_domain_age_days"] = await asyncio.to_thread(_whois_domain_age_days, domain)
    return out


def upsert_social(conn, token_mint: str, s: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO graduation_social
           (token_mint, captured_at, has_twitter, has_telegram, has_website,
            tg_members, website_live, website_status, website_final_url,
            website_domain_age_days)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (token_mint, s["captured_at"], int(s["has_twitter"]), int(s["has_telegram"]),
         int(s["has_website"]), s["tg_members"],
         None if s["website_live"] is None else int(s["website_live"]),
         s["website_status"], s["website_final_url"], s["website_domain_age_days"]),
    )
    conn.commit()
