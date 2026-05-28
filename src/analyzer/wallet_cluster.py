"""Cluster early token buyers by shared funding source and timing.

Algorithm:
  1. For each buyer, fetch SOL transfers IN to that wallet in the 24 h before launch.
  2. Identify the funding source address (the sender of that SOL).
  3. Group buyers whose funding source AND funding timestamp (within a 1-hour window)
     match.
  4. Return WalletCluster objects; CEX-funded wallets are never clustered together.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from src.common.models import TokenBuyer, Wallet, WalletCluster
from src.ingest.helius import CEX_HOT_WALLETS, HeliusClient

_LAMPORTS_PER_SOL = 1_000_000_000


# ── internal helpers ──────────────────────────────────────────────────────────

@dataclass
class _FundingInfo:
    wallet_address: str
    funding_source: str | None  # None means no SOL inflow found in window
    funded_at: int              # timestamp of the funding transfer (0 if unknown)
    sol_funded: float           # SOL received from that source


def _find_sol_inflows(
    wallet_address: str,
    txs: list[dict[str, Any]],
    window_start: int,
    window_end: int,
) -> list[tuple[str, int, float]]:
    """Return (sender, timestamp, sol_amount) for SOL transfers TO wallet_address.

    Only considers transfers whose tx timestamp falls within [window_start, window_end].
    Ignores self-transfers and fee payments.
    """
    inflows: list[tuple[str, int, float]] = []
    for tx in txs:
        ts = tx.get("timestamp", 0)
        if not (window_start <= ts <= window_end):
            continue
        for nt in tx.get("nativeTransfers", []):
            if nt.get("toUserAccount") != wallet_address:
                continue
            sender = nt.get("fromUserAccount", "")
            if not sender or sender == wallet_address:
                continue
            sol = nt.get("amount", 0) / _LAMPORTS_PER_SOL
            if sol > 0:
                inflows.append((sender, ts, sol))
    return inflows


def _largest_inflow(
    inflows: list[tuple[str, int, float]],
) -> tuple[str, int, float] | None:
    """Return the single inflow with the most SOL, or None."""
    if not inflows:
        return None
    return max(inflows, key=lambda t: t[2])


# ── primary async entry point ─────────────────────────────────────────────────

async def build_clusters(
    buyers: list[TokenBuyer],
    token_launch_ts: int,
    helius: HeliusClient,
    *,
    time_window_seconds: int = 3600,
    lookback_seconds: int = 86_400,
) -> list[WalletCluster]:
    """Fetch funding history for each buyer and group into WalletClusters.

    Args:
        buyers: Early buyers for a single token.
        token_launch_ts: Unix epoch of the token's first trade / creation.
        helius: Authenticated HeliusClient instance.
        time_window_seconds: Max spread of funding timestamps to share a cluster.
        lookback_seconds: How far back from launch to look for funding transfers.

    Returns:
        List of WalletCluster; single-member clusters are included when the funder
        is identifiable (skipped when the source is CEX or unknown).
    """
    window_start = token_launch_ts - lookback_seconds
    window_end = token_launch_ts

    async def _enrich(buyer: TokenBuyer) -> _FundingInfo:
        try:
            txs = await helius.get_transactions_for_address(
                buyer.wallet_address, limit=100
            )
        except Exception:
            return _FundingInfo(buyer.wallet_address, None, 0, 0.0)

        inflows = _find_sol_inflows(
            buyer.wallet_address, txs, window_start, window_end
        )
        best = _largest_inflow(inflows)
        if best is None:
            return _FundingInfo(buyer.wallet_address, None, 0, 0.0)

        sender, ts, sol = best
        source = "cex" if sender in CEX_HOT_WALLETS else sender
        return _FundingInfo(buyer.wallet_address, source, ts, sol)

    funding_infos: list[_FundingInfo] = await asyncio.gather(
        *(_enrich(b) for b in buyers)
    )

    return _group_into_clusters(funding_infos, time_window_seconds)


def _group_into_clusters(
    funding_infos: list[_FundingInfo],
    time_window_seconds: int,
) -> list[WalletCluster]:
    """Pure grouping logic: shared funder + timestamps within the time window."""
    # Exclude wallets with no funding info or CEX-funded wallets
    # (CEX wallets are not coordinated even if they share a funder address)
    eligible = [
        fi for fi in funding_infos
        if fi.funding_source and fi.funding_source != "cex"
    ]

    # Group by funding source
    by_source: dict[str, list[_FundingInfo]] = {}
    for fi in eligible:
        by_source.setdefault(fi.funding_source, []).append(fi)  # type: ignore[arg-type]

    clusters: list[WalletCluster] = []

    for source, members in by_source.items():
        # Sort by funding timestamp for greedy windowing
        members.sort(key=lambda fi: fi.funded_at)

        # Slide a window: start new sub-cluster when gap > time_window_seconds
        window: list[_FundingInfo] = []
        for fi in members:
            if not window:
                window.append(fi)
                continue
            window_span = fi.funded_at - window[0].funded_at
            if window_span <= time_window_seconds:
                window.append(fi)
            else:
                # Flush current window
                clusters.append(_make_cluster(source, window))
                window = [fi]
        if window:
            clusters.append(_make_cluster(source, window))

    return clusters


def _make_cluster(source: str, members: list[_FundingInfo]) -> WalletCluster:
    return WalletCluster(
        cluster_id=str(uuid.uuid4()),
        funding_source=source,
        funded_at=members[0].funded_at,
        funded_window_end=members[-1].funded_at,
        member_addresses=[fi.wallet_address for fi in members],
        total_sol_funded=sum(fi.sol_funded for fi in members),
    )


# ── sync helpers (backward compat / pre-enriched data) ───────────────────────

def cluster_buyers(
    buyers: list[TokenBuyer],
    wallets: list[Wallet],
    time_window_seconds: int = 3600,
) -> list[WalletCluster]:
    """Group buyers into clusters using pre-enriched Wallet.funding_source data.

    Each Wallet record must already have funding_source populated.  Use
    build_clusters() instead when funding info hasn't been fetched yet.

    Args:
        buyers: All known buyers for a single token.
        wallets: Enriched Wallet records (must include funding_source and first_seen).
        time_window_seconds: Max spread of funding timestamps in one cluster.

    Returns:
        List of WalletCluster instances.
    """
    wallet_map = {w.address: w for w in wallets}

    funding_infos = [
        _FundingInfo(
            wallet_address=b.wallet_address,
            funding_source=wallet_map[b.wallet_address].funding_source
            if b.wallet_address in wallet_map
            else None,
            funded_at=wallet_map[b.wallet_address].first_seen or 0
            if b.wallet_address in wallet_map
            else 0,
            sol_funded=0.0,
        )
        for b in buyers
    ]

    return _group_into_clusters(funding_infos, time_window_seconds)


def compute_bundle_pct(
    clusters: list[WalletCluster],
    buyers: list[TokenBuyer],
) -> float:
    """Calculate what % of observed buy volume came from any single cluster.

    Returns the maximum per-cluster bundle percentage (0–100).  Uses
    tokens_received if position_size_pct is not populated.
    """
    if not buyers:
        return 0.0

    total_tokens = sum(b.tokens_received for b in buyers)
    if total_tokens == 0:
        return 0.0

    buyer_tokens = {b.wallet_address: b.tokens_received for b in buyers}

    max_pct = 0.0
    for cluster in clusters:
        cluster_tokens = sum(
            buyer_tokens.get(addr, 0.0) for addr in cluster.member_addresses
        )
        pct = (cluster_tokens / total_tokens) * 100
        if pct > max_pct:
            max_pct = pct

    return round(max_pct, 2)
