"""Structural (non-wallet) account exclusion — the missing filter behind every
holder-based metric.

During the bonding curve the pump.fun curve account holds the unsold supply;
after migration the PumpSwap/Raydium pool holds it. Neither is a person. Any
holder list that includes them corrupts team clusters, top-holder percentages,
distribution tracking, and the learning tables downstream.

Two layers:
  STATIC_STRUCTURAL   — global program/authority/burn addresses (mint-independent)
  extract_pool_accounts — per-mint pool/curve accounts pulled from the Solana
                          Tracker /tokens/{mint} response we already fetch

CEX hot wallets remain a separate concern (src/common/cex_wallets.py); callers
that need both union them via structural_set().
"""

STATIC_STRUCTURAL: frozenset[str] = frozenset({
    # burn / system
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022 program
    # pump.fun
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # bonding-curve program
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",   # fee recipient
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",   # migration authority
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",    # PumpSwap AMM program
    # Raydium
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # AMM v4 program
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # AMM v4 authority
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",   # CPMM program
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",   # CPMM vault authority
})

# Keys inside a Solana Tracker pools[] entry whose string values are accounts
# belonging to the pool itself (never a human holder).
_POOL_ACCOUNT_KEYS = (
    "poolId", "bondingCurve", "curve", "tokenAccount", "quoteTokenAccount",
    "baseVault", "quoteVault", "lpMint", "openOrders", "targetOrders",
)

PUMP_FUN_TOTAL_SUPPLY = 1_000_000_000.0   # standard pump.fun mint supply


def extract_pool_accounts(token_info_raw: dict | None) -> set[str]:
    """Per-mint pool/curve accounts from a Solana Tracker token-info response."""
    out: set[str] = set()
    if not isinstance(token_info_raw, dict):
        return out
    for pool in token_info_raw.get("pools") or []:
        if not isinstance(pool, dict):
            continue
        for key in _POOL_ACCOUNT_KEYS:
            v = pool.get(key)
            if isinstance(v, str) and len(v) >= 30:
                out.add(v)
    return out


def extract_total_supply(token_info_raw: dict | None) -> float:
    """Real token supply from token-info; pump.fun default when absent."""
    if isinstance(token_info_raw, dict):
        for pool in token_info_raw.get("pools") or []:
            supply = pool.get("tokenSupply") if isinstance(pool, dict) else None
            try:
                if supply and float(supply) > 0:
                    return float(supply)
            except (TypeError, ValueError):
                continue
    return PUMP_FUN_TOTAL_SUPPLY


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def extract_market_state(token_info_raw: dict | None) -> dict:
    """Point-in-time market state from the token-info response we already fetch.

    NON-RECOVERABLE: liquidity/market-cap/txn state and holder count at the
    graduation instant cannot be re-queried later. Zero extra API cost — the
    token-info call is already made for classification. Values are best-effort;
    missing fields come back None.
    """
    out = {
        "holder_count": None, "liquidity_usd": None, "market_cap_usd": None,
        "price_usd": None, "txns_buys": None, "txns_sells": None, "txns_total": None,
    }
    if not isinstance(token_info_raw, dict):
        return out
    h = token_info_raw.get("holders")
    out["holder_count"] = int(h) if isinstance(h, (int, float)) else None
    # highest-liquidity pool = the live venue
    best, best_liq = None, -1.0
    for p in token_info_raw.get("pools") or []:
        if not isinstance(p, dict):
            continue
        liq = _num((p.get("liquidity") or {}).get("usd"))
        if liq is not None and liq > best_liq:
            best, best_liq = p, liq
    if best:
        out["liquidity_usd"] = _num((best.get("liquidity") or {}).get("usd"))
        out["market_cap_usd"] = _num((best.get("marketCap") or {}).get("usd"))
        out["price_usd"] = _num((best.get("price") or {}).get("usd"))
        txns = best.get("txns") or {}
        out["txns_buys"] = txns.get("buys") if isinstance(txns.get("buys"), int) else None
        out["txns_sells"] = txns.get("sells") if isinstance(txns.get("sells"), int) else None
        out["txns_total"] = txns.get("total") if isinstance(txns.get("total"), int) else None
    return out


def structural_set(
    token_info_raw: dict | None = None,
    cex_addresses: frozenset[str] | set[str] = frozenset(),
    extra: set[str] | None = None,
) -> frozenset[str]:
    """Full exclusion set: static ∪ per-mint pool accounts ∪ CEX ∪ extra."""
    return frozenset(
        STATIC_STRUCTURAL
        | extract_pool_accounts(token_info_raw)
        | set(cex_addresses)
        | (extra or set())
    )


def filter_holders(accounts: list[dict], excluded: frozenset[str] | set[str]) -> list[dict]:
    """Drop structural accounts from a holder list ({address, uiAmount} rows)."""
    return [a for a in accounts if a.get("address") not in excluded]
