"""Top-holder snapshots from FREE Solana RPC — replaces the paid holders endpoint.

Why this exists: the holders call was 16% of Solana Tracker usage, and the platform
gate another 67%. Both are obtainable from standard RPC methods at no cost, which
does two things — cuts paid usage by ~83%, and removes a single vendor's ability to
take the whole pipeline down. When Solana Tracker returned 401 on 2026-08-08, team
detection failed for EVERY coin because holders were unavailable; after this, only
the trade tape depends on the paid API.

Two calls per coin, both free and rotated across endpoints:
  getTokenLargestAccounts -> the 20 largest TOKEN ACCOUNTS (not wallets)
  getMultipleAccounts     -> resolves all 20 to their OWNER wallets in ONE batch

The distinction matters: a token account is an SPL sub-account, and the wallet that
controls it is the entity team detection reasons about. Verified live: 20/20 owners
resolved in a single batched call.

Output shape matches SolanaTrackerClient.get_token_holders exactly —
[{"address": wallet, "uiAmount": float}] — so _parse_bc_holders, the team cluster
builder and holder snapshots need no change.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# getTokenLargestAccounts returns at most 20; that is ample — team detection only
# uses the top holders, and the gate's top-5 concentration reads the head of this list.
MAX_ACCOUNTS = 20

# A real holder is a wallet: its account is owned by the System Program. Pools,
# vaults and program escrows are PDAs owned by their program. Filtering on this is
# generic — it excludes the AMM pool without depending on any vendor's pool list.
# (The previous exclusion set came from Solana Tracker's token_info; when that API
# died the pool was counted as a holder and team supply exceeded 100%.)
SYSTEM_PROGRAM = "11111111111111111111111111111111"


async def _rpc(session, endpoints, method: str, params: list):
    """First endpoint that returns a non-null result wins. None if all fail."""
    import aiohttp
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for url in endpoints:
        if not url:
            continue
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                d = await r.json()
                if d.get("result") is not None:
                    return d["result"]
        except Exception:
            continue
    return None


async def get_token_holders_rpc(session, mint: str,
                                endpoints: list[str] | None = None) -> list[dict]:
    """Top holders as [{address, uiAmount}] from free RPC. [] if unavailable.

    Never raises — the caller falls back to the paid API when this returns empty."""
    if endpoints is None:
        from src.ingest.graduation_monitor import _RPC_ENDPOINTS
        endpoints = _RPC_ENDPOINTS

    largest = await _rpc(session, endpoints, "getTokenLargestAccounts",
                         [mint, {"commitment": "confirmed"}])
    values = (largest or {}).get("value") or []
    if not values:
        return []

    accounts = [v["address"] for v in values[:MAX_ACCOUNTS] if v.get("address")]
    amounts = {v["address"]: float(v.get("uiAmount") or 0.0) for v in values if v.get("address")}
    if not accounts:
        return []

    info = await _rpc(session, endpoints, "getMultipleAccounts",
                      [accounts, {"encoding": "jsonParsed"}])
    parsed = (info or {}).get("value") or []

    candidates: list[dict] = []
    for acct, entry in zip(accounts, parsed):
        owner = None
        try:
            owner = entry["data"]["parsed"]["info"]["owner"]
        except Exception:
            owner = None
        # An unresolvable owner means we cannot attribute the balance to a wallet;
        # dropping it is correct — a token account address must never be treated as
        # a holder wallet (it would pollute team membership with non-wallet entities).
        if owner:
            candidates.append({"address": owner, "uiAmount": amounts.get(acct, 0.0)})
    if not candidates:
        return []

    # Drop program-controlled owners (AMM pool, vaults). One batched call.
    owners = [c["address"] for c in candidates]
    oinfo = await _rpc(session, endpoints, "getMultipleAccounts",
                       [owners, {"encoding": "jsonParsed"}])
    ovals = (oinfo or {}).get("value") or []
    out: list[dict] = []
    for cand, entry in zip(candidates, ovals or [None] * len(candidates)):
        # entry is None for an unfunded wallet — that IS a wallet, so keep it.
        # Only a live account owned by something other than the System Program is a
        # program/PDA and must be excluded.
        if entry is not None and entry.get("owner") not in (None, SYSTEM_PROGRAM):
            continue
        out.append(cand)

    # merge duplicates: one wallet can control several token accounts for a mint
    merged: dict[str, float] = {}
    for row in out:
        merged[row["address"]] = merged.get(row["address"], 0.0) + row["uiAmount"]
    return [{"address": a, "uiAmount": v}
            for a, v in sorted(merged.items(), key=lambda kv: -kv[1])]


async def get_token_holders_resilient(session, mint: str, st_client=None) -> tuple[list[dict], str]:
    """RPC first, paid API only as fallback. Returns (holders, source_used)."""
    try:
        rows = await get_token_holders_rpc(session, mint)
        if rows:
            return rows, "rpc"
    except Exception as exc:
        logger.debug("rpc holders failed for %s: %s", mint[:8], exc)

    if st_client is not None:
        try:
            rows = await st_client.get_token_holders(mint)
            if rows:
                return rows, "solana_tracker"
        except Exception as exc:
            logger.debug("ST holders fallback failed for %s: %s", mint[:8], exc)
    return [], "none"
