"""Async fire-and-forget sync layer: SQLite (Mac mini) → Supabase (dashboard).

Design principles:
  - Never blocks the analysis pipeline. Every sync call is wrapped in
    asyncio.create_task() so the hot path (WebSocket → Helius → verdict) is
    unaffected even if Supabase is slow or unreachable.
  - Graceful degradation. If SUPABASE_URL / SUPABASE_SERVICE_KEY are blank,
    all functions are silent no-ops. The bot runs SQLite-only.
  - Resilient. All Supabase calls catch every exception and log at DEBUG level
    so a network blip never surfaces as an analysis error.

Usage pattern in callers:
    from src.common.supabase_sync import push
    asyncio.create_task(push.graduation_event(event, verdict, confidence))
"""

import asyncio
import logging
from typing import Any

from src.common.config import settings

logger = logging.getLogger(__name__)

# Module-level cached async client — created once on first use.
_client = None


def _get_client():
    """Return an initialised supabase AsyncClient, or None if not configured."""
    global _client
    if _client is not None:
        return _client
    if not settings.supabase_url or not settings.supabase_service_key:
        return None
    try:
        from supabase import create_client
        _client = create_client(settings.supabase_url, settings.supabase_service_key)
        logger.info("supabase sync enabled → %s", settings.supabase_url[:40])
        return _client
    except Exception as exc:
        logger.debug("supabase client init failed: %s", exc)
        return None


def _upsert(table: str, record: dict[str, Any]) -> None:
    """Synchronous upsert — run via asyncio.to_thread to avoid blocking the loop."""
    client = _get_client()
    if client is None:
        return
    try:
        client.table(table).upsert(record, on_conflict="token_mint").execute()
    except Exception as exc:
        logger.debug("supabase upsert %s failed: %s", table, exc)


def _upsert_keyed(table: str, record: dict[str, Any], conflict_col: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.table(table).upsert(record, on_conflict=conflict_col).execute()
    except Exception as exc:
        logger.debug("supabase upsert %s failed: %s", table, exc)


async def _run(table: str, record: dict[str, Any], conflict_col: str = "token_mint") -> None:
    await asyncio.to_thread(_upsert_keyed, table, record, conflict_col)


# ── Public sync functions (one per table) ─────────────────────────────────────
# Each returns a coroutine — callers should wrap in asyncio.create_task().

async def token(mint: str, symbol: str, name: str, created_at: int) -> None:
    await _run("tokens", {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "launchpad": "pump.fun",
        "created_at": created_at,
    }, conflict_col="mint")


async def graduation_event(
    token_mint: str,
    graduated_at: int,
    detection_lag_seconds: int,
    structural_verdict: str,
    verdict_confidence: float,
    pumpswap_pool_address: str | None = None,
    bc_top_holders_json: list | None = None,
) -> None:
    import json
    await _run("graduation_events", {
        "token_mint": token_mint,
        "graduated_at": graduated_at,
        "detection_lag_seconds": detection_lag_seconds,
        "structural_verdict": structural_verdict,
        "verdict_confidence": round(verdict_confidence, 4),
        "pumpswap_pool_address": pumpswap_pool_address,
        "bc_top_holders_json": bc_top_holders_json or [],
    })


async def team_cluster(
    cluster_id: str,
    token_mint: str,
    funding_source: str | None,
    member_addresses: list[str],
    supply_pct_at_graduation: float,
    first_buy_offset_seconds: float,
    is_bc_sniper: bool,
) -> None:
    await _run("team_clusters", {
        "cluster_id": cluster_id,
        "token_mint": token_mint,
        "funding_source": funding_source,
        "member_addresses": member_addresses,
        "supply_pct_at_graduation": round(supply_pct_at_graduation, 4),
        "first_buy_offset_seconds": round(first_buy_offset_seconds, 2),
        "is_bc_sniper": is_bc_sniper,
    }, conflict_col="cluster_id")


async def coin_outcome(
    token_mint: str,
    check_offset_h: int,
    checked_at: int,
    mc_usd: float | None,
    price_change_pct: float | None,
    classified: str | None,
) -> None:
    await _run("coin_outcomes", {
        "token_mint": token_mint,
        "check_offset_h": check_offset_h,
        "checked_at": checked_at,
        "mc_usd": mc_usd,
        "price_change_pct": price_change_pct,
        "classified": classified,
    }, conflict_col="token_mint,check_offset_h")


async def post_grad_behavior(
    token_mint: str,
    check_offset_h: int,
    checked_at: int,
    holders_remaining_count: int | None,
    team_sold_pct: float | None,
    distribution_signal: str,
) -> None:
    await _run("post_grad_behavior", {
        "token_mint": token_mint,
        "check_offset_h": check_offset_h,
        "checked_at": checked_at,
        "holders_remaining_count": holders_remaining_count,
        "team_sold_pct": team_sold_pct,
        "distribution_signal": distribution_signal,
    }, conflict_col="token_mint,check_offset_h")


async def funder_reputation(
    funding_source: str,
    rug_count: int,
    moon_count: int,
    ok_count: int,
    rug_rate: float,
    moon_rate: float,
    avg_bundle_pct: float,
    avg_dev_pct: float,
    last_seen: int,
    is_known_rugger: bool,
    graduated_mints: list[str],
) -> None:
    await _run("funder_reputation", {
        "funding_source": funding_source,
        "graduated_mints": graduated_mints,
        "rug_count": rug_count,
        "moon_count": moon_count,
        "ok_count": ok_count,
        "rug_rate": round(rug_rate, 4),
        "moon_rate": round(moon_rate, 4),
        "avg_bundle_pct": round(avg_bundle_pct, 4),
        "avg_dev_pct": round(avg_dev_pct, 4),
        "last_seen": last_seen,
        "is_known_rugger": is_known_rugger,
    }, conflict_col="funding_source")
