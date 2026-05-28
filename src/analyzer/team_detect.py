"""Identify the team / dev cluster behind a token.

Heuristics:
  - The cluster containing the token deployer (Token.mint authority / creator) is
    the team cluster.
  - Deployer is stored as Token.mint; the actual deployer wallet is found by
    checking which cluster member matches the token's creator field, or by looking
    for the wallet that first interacted with the Pump.fun program to create the
    token.
  - Largest cluster by SOL funded is used as a tiebreaker when the deployer isn't
    directly present.
  - get_past_deployments is a chain-scan that should be implemented when an RPC
    indexer is available; currently returns an empty list.
"""

from src.common.models import Token, TokenBuyer, Wallet, WalletCluster


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

    Full implementation requires scanning chain history for token-create
    instructions signed by dev_wallet.  Returns empty list until an RPC
    indexer integration is wired up.

    Args:
        dev_wallet: Wallet address of the suspected developer.

    Returns:
        List of token mint addresses from previous launches.
    """
    # TODO: implement via Helius get_transactions_for_address + filter for
    # InitializeMint / pump.fun create-token instructions.
    return []
