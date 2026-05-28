"""Integration tests for wallet_cluster and team_detect.

All Helius API calls are replaced by pre-recorded fixture responses stored in
tests/fixtures/cluster_responses.json — no real network traffic.

Scenario (see fixture for full detail):
  - 7 buyers for a Pump.fun rug-pull token.
  - 4 team wallets (deployer + TEAM1/2/3) were all funded from the same wallet
    (FUNDER) within a 25-minute window ~30-55 min before token launch.
  - RETAIL1 was funded by Coinbase (CEX) → excluded from clusters.
  - RETAIL2 was funded by an independent wallet → its own single-member cluster.
  - RETAIL3 has no SOL inflow in the 24 h window → excluded from clusters.
  - Expected: 1 team cluster (4 members), 1 retail cluster (1 member RETAIL2).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analyzer.team_detect import (
    compute_dev_pct,
    get_past_deployments,
    identify_team_cluster,
)
from src.analyzer.wallet_cluster import (
    build_clusters,
    cluster_buyers,
    compute_bundle_pct,
)
from src.common.models import Token, TokenBuyer, Wallet, WalletCluster

# ── load fixture ──────────────────────────────────────────────────────────────

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_responses.json").read_text()
)
_SCENARIO = _FIXTURE["scenario"]
_ADDRS = _FIXTURE["addresses"]
_TXS_BY_WALLET = _FIXTURE["transactions_by_wallet"]

DEPLOYER = _ADDRS["deployer"]
TEAM1    = _ADDRS["team1"]
TEAM2    = _ADDRS["team2"]
TEAM3    = _ADDRS["team3"]
RETAIL1  = _ADDRS["retail_cex"]
RETAIL2  = _ADDRS["retail_ind"]
RETAIL3  = _ADDRS["retail_nof"]
FUNDER   = _ADDRS["funder"]
TOKEN    = _SCENARIO["token_mint"]
LAUNCH   = _SCENARIO["token_launch_ts"]

# ── shared buyers ─────────────────────────────────────────────────────────────

BUYERS = [
    TokenBuyer(TOKEN, DEPLOYER, LAUNCH, sol_amount=5.0,  tokens_received=300_000),
    TokenBuyer(TOKEN, TEAM1,    LAUNCH, sol_amount=5.0,  tokens_received=250_000),
    TokenBuyer(TOKEN, TEAM2,    LAUNCH, sol_amount=4.0,  tokens_received=150_000),
    TokenBuyer(TOKEN, TEAM3,    LAUNCH, sol_amount=3.0,  tokens_received=100_000),
    TokenBuyer(TOKEN, RETAIL1,  LAUNCH, sol_amount=0.5,  tokens_received= 50_000),
    TokenBuyer(TOKEN, RETAIL2,  LAUNCH, sol_amount=0.75, tokens_received= 60_000),
    TokenBuyer(TOKEN, RETAIL3,  LAUNCH, sol_amount=0.6,  tokens_received= 40_000),
]
# total tokens = 950_000; team tokens = 800_000 ≈ 84.2 %


@pytest.fixture
def helius():
    """Mock HeliusClient that returns fixture data without network calls."""
    client = MagicMock()
    client.get_transactions_for_address = AsyncMock(
        side_effect=lambda addr, limit=100: _TXS_BY_WALLET.get(addr, [])
    )
    return client


# ── build_clusters integration ────────────────────────────────────────────────

async def test_build_clusters_identifies_team_cluster(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    # Should produce exactly 2 clusters: team (4 members) + retail2 (1 member)
    # RETAIL1 is CEX-funded, RETAIL3 has no inflow in window → both excluded
    assert len(clusters) == 2

    sizes = sorted(len(c.member_addresses) for c in clusters)
    assert sizes == [1, 4]


async def test_build_clusters_team_cluster_has_all_four_wallets(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))
    assert len(team_cluster.member_addresses) == 4
    assert DEPLOYER in team_cluster.member_addresses
    assert TEAM1    in team_cluster.member_addresses
    assert TEAM2    in team_cluster.member_addresses
    assert TEAM3    in team_cluster.member_addresses


async def test_build_clusters_team_cluster_funder_address(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))
    assert team_cluster.funding_source == FUNDER


async def test_build_clusters_window_timestamps(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))
    # Earliest fund: TEAM1 at 1705309500, latest: DEPLOYER at 1705311000
    assert team_cluster.funded_at == 1705309500
    assert team_cluster.funded_window_end == 1705311000
    # 25-minute spread, well within the 1-hour window
    assert team_cluster.funded_window_end - team_cluster.funded_at < 3600


async def test_build_clusters_total_sol_funded(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))
    # 4 × 5 SOL each funded = 20 SOL
    assert team_cluster.total_sol_funded == pytest.approx(20.0)


async def test_build_clusters_retail2_solo_cluster(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    solo_cluster = min(clusters, key=lambda c: len(c.member_addresses))
    assert solo_cluster.member_addresses == [RETAIL2]
    assert solo_cluster.funding_source == _ADDRS["ind_funder"]


async def test_build_clusters_narrow_window_splits_team(helius):
    # With a 5-minute window the 4 team wallets (25-min spread) should split
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=300)

    team_funder_clusters = [c for c in clusters if c.funding_source == FUNDER]
    # 25-min spread with 5-min windows → multiple sub-clusters
    assert len(team_funder_clusters) > 1


async def test_build_clusters_helius_called_once_per_buyer(helius):
    await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    assert helius.get_transactions_for_address.call_count == len(BUYERS)


async def test_build_clusters_graceful_on_empty_buyer_list(helius):
    clusters = await build_clusters([], LAUNCH, helius, time_window_seconds=3600)
    assert clusters == []


# ── compute_bundle_pct ────────────────────────────────────────────────────────

async def test_compute_bundle_pct_team_cluster(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)

    pct = compute_bundle_pct(clusters, BUYERS)
    # Team bought 800_000 / 950_000 ≈ 84.2 %
    assert pct == pytest.approx(84.21, abs=0.5)


def test_compute_bundle_pct_empty_buyers():
    cluster = WalletCluster(
        cluster_id="x", funding_source="F", member_addresses=[TEAM1]
    )
    assert compute_bundle_pct([cluster], []) == 0.0


def test_compute_bundle_pct_no_cluster_members_in_buyers():
    cluster = WalletCluster(
        cluster_id="x", funding_source="F", member_addresses=["NOT_A_BUYER"]
    )
    buyers = [TokenBuyer(TOKEN, TEAM1, LAUNCH, sol_amount=1.0, tokens_received=100)]
    assert compute_bundle_pct([cluster], buyers) == 0.0


# ── cluster_buyers (sync, pre-enriched wallets) ───────────────────────────────

def test_cluster_buyers_sync_groups_by_funding_source():
    wallets = [
        Wallet(DEPLOYER, funding_source=FUNDER, first_seen=1705311000),
        Wallet(TEAM1,    funding_source=FUNDER, first_seen=1705309500),
        Wallet(TEAM2,    funding_source=FUNDER, first_seen=1705309800),
        Wallet(TEAM3,    funding_source=FUNDER, first_seen=1705310700),
        Wallet(RETAIL2,  funding_source=_ADDRS["ind_funder"], first_seen=1705305600),
    ]
    buyers = [b for b in BUYERS if b.wallet_address != RETAIL1 and b.wallet_address != RETAIL3]

    clusters = cluster_buyers(buyers, wallets, time_window_seconds=3600)

    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))
    assert len(team_cluster.member_addresses) == 4
    assert team_cluster.funding_source == FUNDER


def test_cluster_buyers_excludes_cex_funded():
    wallets = [
        Wallet(TEAM1,   funding_source=FUNDER,  first_seen=1705309500),
        Wallet(RETAIL1, funding_source="cex",   first_seen=1705226400),
    ]
    buyers = [
        TokenBuyer(TOKEN, TEAM1,   LAUNCH, sol_amount=5.0, tokens_received=300_000),
        TokenBuyer(TOKEN, RETAIL1, LAUNCH, sol_amount=0.5, tokens_received= 50_000),
    ]

    clusters = cluster_buyers(buyers, wallets, time_window_seconds=3600)

    # Only TEAM1 should form a cluster; RETAIL1 (cex) is excluded
    all_members = [addr for c in clusters for addr in c.member_addresses]
    assert RETAIL1 not in all_members


# ── identify_team_cluster ─────────────────────────────────────────────────────

async def test_identify_team_cluster_by_deployer(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)
    token = Token(TOKEN, "RUG", "Rug Token", "pump.fun", LAUNCH)

    team = identify_team_cluster(token, clusters, wallets=[], deployer=DEPLOYER)

    assert team is not None
    assert DEPLOYER in team.member_addresses
    assert team.is_likely_team is True


async def test_identify_team_cluster_largest_fallback(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)
    token = Token(TOKEN, "RUG", "Rug Token", "pump.fun", LAUNCH)

    # No deployer hint — should fall back to largest cluster
    team = identify_team_cluster(token, clusters, wallets=[], deployer=None)

    assert team is not None
    assert len(team.member_addresses) == 4
    assert team.is_likely_team is True


async def test_identify_team_cluster_dev_label(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)
    token = Token(TOKEN, "RUG", "Rug Token", "pump.fun", LAUNCH)
    wallets = [Wallet(TEAM1, label="dev wallet")]

    team = identify_team_cluster(token, clusters, wallets=wallets, deployer=None)

    assert team is not None
    assert TEAM1 in team.member_addresses


def test_identify_team_cluster_empty_clusters():
    token = Token(TOKEN, "RUG", "Rug Token", "pump.fun", LAUNCH)
    assert identify_team_cluster(token, [], wallets=[], deployer=DEPLOYER) is None


# ── compute_dev_pct ───────────────────────────────────────────────────────────

async def test_compute_dev_pct_team_cluster(helius):
    clusters = await build_clusters(BUYERS, LAUNCH, helius, time_window_seconds=3600)
    team_cluster = max(clusters, key=lambda c: len(c.member_addresses))

    pct = compute_dev_pct(team_cluster, BUYERS)
    # Team tokens: 300_000 + 250_000 + 150_000 + 100_000 = 800_000
    # Total:       800_000 + 50_000 + 60_000 + 40_000   = 950_000
    # Dev pct:     800_000 / 950_000 ≈ 84.21 %
    assert pct == pytest.approx(84.21, abs=0.5)


def test_compute_dev_pct_empty_buyers():
    cluster = WalletCluster(
        cluster_id="x", funding_source="F", member_addresses=[TEAM1]
    )
    assert compute_dev_pct(cluster, []) == 0.0


# ── get_past_deployments ──────────────────────────────────────────────────────

def test_get_past_deployments_returns_list():
    result = get_past_deployments(DEPLOYER)
    assert isinstance(result, list)
