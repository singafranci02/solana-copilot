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


def get_past_deployments(dev_wallet: str, conn=None) -> list[str]:
    """Mint addresses of other tokens previously deployed by dev_wallet.

    Self-learned from tokens.creator_wallet (captured at graduation from the
    token-info creation field) — no external API. Empty without a connection.
    """
    if conn is None or not dev_wallet:
        return []
    rows = conn.execute(
        "SELECT mint FROM tokens WHERE creator_wallet = ? ORDER BY created_at",
        (dev_wallet,),
    ).fetchall()
    return [r["mint"] for r in rows]


# ── Graduation-context cluster detection ─────────────────────────────────────

# Evidence-component weights (sum to 1.0) and the coordination edge weights used
# inside E_coord's noisy-OR. Tuned so a plain buyer∩holder wallet lands exactly
# at the member threshold — reproducing v1 recall when no extra evidence exists.
_W_OVERLAP, _W_COORD, _W_FUNDING, _W_FRESH, _W_SNIPE = 0.35, 0.30, 0.20, 0.05, 0.10
_MEMBER_THRESHOLD = 0.35
_PERIPHERAL_THRESHOLD = 0.20
_COORD_EDGE_WEIGHT = {
    "funder": 0.9, "same_slot_real": 0.85, "same_slot": 0.7,
    "buy_size": 0.35, "lockstep_sell": 0.35, "behavioral": 0.5,
}
_EARLY_BUYER_OFFSET_S = 120.0


def score_team_membership(
    buyers: list[TokenBuyer],
    bc_top_holders: list[dict],
    excluded: frozenset[str] | set[str],
    *,
    entity_edges: dict[str, set[str]] | None = None,   # wallet → its launch-entity edge_sources
    funder_by_wallet: dict[str, str | None] | None = None,
    creator_wallet: str | None = None,
    creator_funder: str | None = None,
    first_seen: dict[str, int] | None = None,
    sig_count: dict[str, int] | None = None,
    slot_offset: dict[str, int] | None = None,         # wallet → min slot_offset_from_first
    graduated_at: int | None = None,
) -> dict[str, tuple[float, dict]]:
    """Per-wallet team-membership evidence score ∈ [0,1] with an evidence record.

    Pure. With all optional maps empty, only E_overlap fires, so buyer∩holder
    wallets score exactly _MEMBER_THRESHOLD (v1-equivalent). Candidate universe =
    BC buyers ∪ eligible holders ∪ launch coordination-entity members.
    """
    entity_edges = entity_edges or {}
    funder_by_wallet = funder_by_wallet or {}
    first_seen = first_seen or {}
    sig_count = sig_count or {}
    slot_offset = slot_offset or {}

    holder_map = {h["wallet"]: h["pct"] for h in bc_top_holders
                  if h.get("wallet") and h["wallet"] not in excluded}
    top5 = {h["wallet"] for h in bc_top_holders[:5] if h.get("wallet") and h["wallet"] not in excluded}
    buyer_set = {b.wallet_address for b in buyers if b.wallet_address not in excluded}
    first_buy = {}
    for b in buyers:
        prev = first_buy.get(b.wallet_address)
        if prev is None or b.bought_at < prev:
            first_buy[b.wallet_address] = b.bought_at
    launch_ts = min((b.bought_at for b in buyers), default=None)

    # candidate funder-sharing count (for E_funding 0.7 tier)
    funder_counts: dict[str, int] = {}
    for w, f in funder_by_wallet.items():
        if f and f != "cex":
            funder_counts[f] = funder_counts.get(f, 0) + 1

    candidates = buyer_set | set(holder_map) | set(entity_edges)
    scores: dict[str, tuple[float, dict]] = {}
    for w in candidates:
        if w in excluded:
            continue
        ev: dict = {}

        # E_overlap
        if w in buyer_set and w in holder_map:
            e_overlap = 1.0
        elif w in top5:
            e_overlap = 0.5
        elif w in buyer_set and launch_ts is not None and first_buy.get(w, 1 << 62) - launch_ts <= _EARLY_BUYER_OFFSET_S:
            e_overlap = 0.3
        else:
            e_overlap = 0.0
        ev["overlap"] = e_overlap

        # E_coord — noisy-OR over the wallet's launch-entity edge sources
        edges = entity_edges.get(w, set())
        if edges:
            prod = 1.0
            for lbl in edges:
                prod *= (1.0 - _COORD_EDGE_WEIGHT.get(lbl, 0.3))
            e_coord = round(1.0 - prod, 4)
            ev["coord"] = e_coord
            ev["coord_edges"] = sorted(edges)
        else:
            e_coord = 0.0

        # E_funding — insider fingerprint (funded by creator or creator's funder)
        f = funder_by_wallet.get(w)
        if f == "cex":
            e_funding = 0.0
        elif f and creator_wallet and (f == creator_wallet or (creator_funder and f == creator_funder)):
            e_funding = 1.0
            ev["funding"] = "creator_linked"
        elif f and funder_counts.get(f, 0) >= 2:
            e_funding = 0.7
            ev["funding"] = "shared_funder"
        else:
            e_funding = 0.0

        # E_fresh — young wallet + narrow history = network infrastructure prior
        e_fresh = 0.0
        if graduated_at and w in first_seen:
            age = graduated_at - first_seen[w]
            e_age = 1.0 if age < 24 * 3600 else (0.5 if age < 72 * 3600 else 0.0)
            sc = sig_count.get(w)
            e_narrow = 0.0 if sc is None else (1.0 if sc < 10 else (0.5 if sc < 50 else 0.0))
            e_fresh = 0.5 * e_age + 0.5 * e_narrow

        # E_snipe — launch-block buy (Phase B)
        off = slot_offset.get(w)
        e_snipe = 0.0 if off is None else (1.0 if off == 0 else (0.7 if off <= 3 else 0.0))
        if off is not None:
            ev["slot_offset"] = off

        score = round(
            _W_OVERLAP * e_overlap + _W_COORD * e_coord + _W_FUNDING * e_funding
            + _W_FRESH * e_fresh + _W_SNIPE * e_snipe, 4
        )
        if score > 0:
            scores[w] = (score, ev)
    return scores


def build_team_cluster_post_grad(
    token_mint: str,
    buyers: list[TokenBuyer],
    bc_top_holders: list[dict],
    cex_addresses: frozenset[str],
    structural_addresses: frozenset[str] = frozenset(),
    *,
    entity_edges: dict[str, set[str]] | None = None,
    funder_by_wallet: dict[str, str | None] | None = None,
    creator_wallet: str | None = None,
    creator_funder: str | None = None,
    first_seen: dict[str, int] | None = None,
    sig_count: dict[str, int] | None = None,
    slot_offset: dict[str, int] | None = None,
    graduated_at: int | None = None,
) -> tuple[TeamCluster | None, dict[str, tuple[float, dict]]]:
    """Identify the team cluster using probabilistic per-wallet evidence scoring.

    Fuses buyer∩holder overlap, launch coordination-entity co-membership (with
    edge_sources weighting), creator/shared funding (the insider fingerprint),
    wallet freshness, and launch-slot snipes (see score_team_membership). With
    no optional evidence maps this reduces to v1 (buyer∩holder, top-5 fallback).

    Returns (TeamCluster | None, scored) where scored is wallet → (score, evidence)
    for every candidate — persisted to team_members by the caller.
    """
    excluded = cex_addresses | structural_addresses
    eligible_holders = [
        h for h in bc_top_holders
        if h.get("wallet") and h["wallet"] not in excluded
    ]
    if not eligible_holders:
        return None, {}

    holder_map = {h["wallet"]: h["pct"] for h in eligible_holders}

    scored = score_team_membership(
        buyers, bc_top_holders, excluded,
        entity_edges=entity_edges, funder_by_wallet=funder_by_wallet,
        creator_wallet=creator_wallet, creator_funder=creator_funder,
        first_seen=first_seen, sig_count=sig_count, slot_offset=slot_offset,
        graduated_at=graduated_at,
    )

    members = [w for w, (sc, _) in scored.items() if sc >= _MEMBER_THRESHOLD]
    if members:
        team_candidates = members
        fallback = False
    else:
        # Nothing crossed threshold — v1 top-5 clean-holder fallback
        team_candidates = [h["wallet"] for h in eligible_holders[:5]]
        fallback = True
        for w in team_candidates:
            scored.setdefault(w, (_PERIPHERAL_THRESHOLD, {"fallback_top_holder": True}))

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

    cluster = TeamCluster(
        cluster_id=str(uuid.uuid4()),
        token_mint=token_mint,
        funding_source=None,   # resolved separately via wallet funding lookup
        member_addresses=team_candidates,
        supply_pct_at_graduation=round(supply_pct, 2),
        first_buy_offset_seconds=first_buy_offset,
        is_bc_sniper=is_sniper,
    )
    return cluster, scored


def upsert_team_members(
    conn, token_mint: str, scored: dict[str, tuple[float, dict]],
    member_set: set[str],
) -> None:
    """Persist per-wallet team-membership scores + evidence."""
    import json
    import time
    if not scored:
        return
    now = int(time.time())
    conn.executemany(
        """INSERT INTO team_members (token_mint, wallet, score, is_member, evidence_json, computed_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, wallet) DO UPDATE SET
               score=excluded.score, is_member=excluded.is_member,
               evidence_json=excluded.evidence_json, computed_at=excluded.computed_at""",
        [
            (token_mint, w, sc, int(w in member_set), json.dumps(ev), now)
            for w, (sc, ev) in scored.items()
        ],
    )
    conn.commit()
