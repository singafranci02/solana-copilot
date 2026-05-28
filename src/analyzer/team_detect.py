"""Identify the team / dev cluster behind a token.

Two analysis contexts:

  Launch-time (Pump.fun BC phase):
    identify_team_cluster — matches buyers against known deployer address and
    dev-labelled wallet records. Used by pump_monitor.

  Graduation-time (post-PumpSwap migration):
    build_team_cluster_post_grad — uses the Helius top-holders snapshot taken
    at graduation to find wallets that accumulated during BC and still hold at
    migration. Richer signal than launch-time heuristics. Used by graduation_monitor.
"""

import uuid

from src.common.models import TeamCluster, Token, TokenBuyer, Wallet, WalletCluster


def identify_team_cluster(
    token: Token,
    clusters: list[WalletCluster],
    wallets: list[Wallet],
    *,
    deployer: str | None = None,
) -> WalletCluster | None:
    """Return the cluster most likely to represent the token's launch team.

    Identification priority:
      1. Cluster containing `deployer` (if provided).
      2. Cluster whose member has a wallet label containing "dev" or "deployer".
      3. Largest cluster by member count (the team typically coordinates the most
         wallets).

    Marks the returned cluster's is_likely_team flag True in place.

    Args:
        token: The token being analysed.
        clusters: All detected buyer clusters for this token.
        wallets: Enriched Wallet records; labels are inspected for dev signals.
        deployer: Known deployer wallet address, if available.

    Returns:
        The team WalletCluster (mutated with is_likely_team=True), or None.
    """
    if not clusters:
        return None

    wallet_label = {w.address: (w.label or "").lower() for w in wallets}

    def _score(cluster: WalletCluster) -> tuple[int, int, float]:
        # Returns a tuple that sorts descending: (deployer_match, dev_label_match, size/sol)
        has_deployer = (
            int(deployer in cluster.member_addresses) if deployer else 0
        )
        has_dev_label = int(
            any(
                "dev" in wallet_label.get(addr, "") or
                "deployer" in wallet_label.get(addr, "")
                for addr in cluster.member_addresses
            )
        )
        return (has_deployer, has_dev_label, len(cluster.member_addresses))

    team = max(clusters, key=_score)

    # Only flag if we have at least one positive signal; a lone cluster with no
    # labels and no known deployer is still returned but not blindly trusted.
    team.is_likely_team = True
    return team


def compute_dev_pct(
    team_cluster: WalletCluster,
    buyers: list[TokenBuyer],
) -> float:
    """Calculate what % of observed buy volume was purchased by the team cluster.

    Args:
        team_cluster: Output of identify_team_cluster.
        buyers: TokenBuyer records for the token.

    Returns:
        Float between 0 and 100 representing dev-held %.
    """
    if not buyers:
        return 0.0

    total_tokens = sum(b.tokens_received for b in buyers)
    if total_tokens == 0:
        return 0.0

    team_addrs = set(team_cluster.member_addresses)
    team_tokens = sum(
        b.tokens_received for b in buyers if b.wallet_address in team_addrs
    )
    return round((team_tokens / total_tokens) * 100, 2)


def get_past_deployments(dev_wallet: str) -> list[str]:
    """Return mint addresses of other tokens previously deployed by dev_wallet.

    Returns empty list until Helius indexer integration is wired up.
    TODO: implement via get_transactions_for_address + InitializeMint filter.
    """
    return []


# ── Graduation-context cluster detection ─────────────────────────────────────

def build_team_cluster_post_grad(
    token_mint: str,
    buyers: list[TokenBuyer],
    bc_top_holders: list[dict],
    cex_addresses: frozenset[str],
) -> TeamCluster | None:
    """Identify the team cluster using graduation-time holder data.

    Strategy:
      1. From bc_top_holders (Helius snapshot at graduation), exclude CEX wallets.
      2. Cross-reference with BC-phase buyers: wallets that both bought early AND
         still hold at graduation are the highest-confidence team/sniper candidates.
      3. If no overlap found, fall back to top non-CEX holders directly.
      4. Compute supply_pct_at_graduation from the holder snapshot.
      5. Detect BC snipers: did the earliest team buy happen within 30s of launch?

    Args:
        token_mint: Mint address of the graduated token.
        buyers: TokenBuyer records collected during BC phase.
        bc_top_holders: [{wallet, pct, ui_amount}] from Helius at graduation.
        cex_addresses: Known CEX hot wallet addresses to exclude.

    Returns:
        TeamCluster or None if no plausible team can be identified.
    """
    eligible_holders = [
        h for h in bc_top_holders
        if h.get("wallet") and h["wallet"] not in cex_addresses
    ]
    if not eligible_holders:
        return None

    holder_map = {h["wallet"]: h["pct"] for h in eligible_holders}
    buyer_set = {b.wallet_address for b in buyers if b.wallet_address not in cex_addresses}

    # Wallets that bought in BC phase AND still hold at graduation
    overlap = [addr for addr in buyer_set if addr in holder_map]

    if overlap:
        team_candidates = overlap
    else:
        # No buyers matched top holders — use top-5 non-CEX holders as candidates
        team_candidates = [h["wallet"] for h in eligible_holders[:5]]

    supply_pct = sum(holder_map.get(addr, 0.0) for addr in team_candidates)

    # Sniper detection: earliest team-member buy within 30s of first observed buy
    first_buy_offset = 0.0
    is_sniper = False
    if buyers and team_candidates:
        candidate_set = set(team_candidates)
        candidate_buys = [b for b in buyers if b.wallet_address in candidate_set]
        if candidate_buys and len(buyers) > 0:
            launch_proxy_ts = min(b.bought_at for b in buyers)
            first_team_ts = min(b.bought_at for b in candidate_buys)
            first_buy_offset = float(max(0, first_team_ts - launch_proxy_ts))
            is_sniper = first_buy_offset <= 30.0

    return TeamCluster(
        cluster_id=str(uuid.uuid4()),
        token_mint=token_mint,
        funding_source=None,   # resolved separately via wallet funding lookup
        member_addresses=team_candidates,
        supply_pct_at_graduation=round(supply_pct, 2),
        first_buy_offset_seconds=first_buy_offset,
        is_bc_sniper=is_sniper,
    )
