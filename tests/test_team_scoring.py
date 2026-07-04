"""Phase A: probabilistic team-membership scoring evidence fusion."""

from src.analyzer.team_detect import (
    score_team_membership, build_team_cluster_post_grad,
    _MEMBER_THRESHOLD,
)
from src.common.models import TokenBuyer

MINT = "M" * 44
BUYER = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
HOLDER = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"
EXITED = "7s1da8DduuBFqGra5bJBjpnvL5E9mGzCuMk1Qkh4or2Z"   # entity member, already sold
CREATOR = "9DrvZvyWh1HuAoZxvYWMvkf2XCzryCpGgHqrMjyDWpmo"
FUNDER = "ECHhYtSogLASVDZ8NTg1w7oCo2aeJGnNu4pDNvorwB9a"


def _buyer(w, bought_at=1000):
    return TokenBuyer(token_mint=MINT, wallet_address=w, bought_at=bought_at,
                      sol_amount=1.0, tokens_received=1e6)


def _holder(w, pct):
    return {"wallet": w, "pct": pct, "ui_amount": pct * 1e7}


def test_buyer_holder_overlap_exactly_at_threshold():
    """v1-equivalent: buyer∩holder with no other evidence == member threshold."""
    scored = score_team_membership([_buyer(BUYER)], [_holder(BUYER, 10.0)], frozenset())
    assert scored[BUYER][0] == _MEMBER_THRESHOLD
    assert scored[BUYER][0] >= _MEMBER_THRESHOLD   # is a member


def test_funder_entity_exited_wallet_becomes_member():
    """A wallet that exited before graduation (not a holder) but is in a funder-
    linked launch entity should still be recovered — v1 missed these entirely."""
    scored = score_team_membership(
        [_buyer(EXITED)], [_holder(HOLDER, 10.0)], frozenset(),
        entity_edges={EXITED: {"funder", "same_slot_real"}},
    )
    # E_coord noisy-OR(0.9, 0.85) = 0.985 → 0.30*0.985 ≈ 0.296; buyer-only early
    # adds 0.35*0.3 = 0.105 → ~0.40 ≥ threshold
    assert scored[EXITED][0] >= _MEMBER_THRESHOLD
    assert "funder" in scored[EXITED][1]["coord_edges"]


def test_funded_by_creator_is_insider_fingerprint():
    scored = score_team_membership(
        [_buyer(BUYER)], [], frozenset(),
        funder_by_wallet={BUYER: CREATOR}, creator_wallet=CREATOR,
    )
    # buyer-only early (0.105) + funding 1.0 (0.20) = 0.305 → peripheral,
    # crosses to member once it's also a holder; here confirms funding evidence fires
    assert scored[BUYER][1]["funding"] == "creator_linked"


def test_cex_funder_gives_no_funding_evidence():
    scored = score_team_membership(
        [_buyer(BUYER)], [_holder(BUYER, 10.0)], frozenset(),
        funder_by_wallet={BUYER: "cex"},
    )
    assert "funding" not in scored[BUYER][1]        # cex → 0 funding evidence


def test_launch_slot_snipe_evidence():
    scored = score_team_membership(
        [_buyer(BUYER)], [_holder(BUYER, 10.0)], frozenset(),
        slot_offset={BUYER: 0},
    )
    # overlap 1.0 (0.35) + snipe 1.0 (0.10) = 0.45
    assert scored[BUYER][0] == 0.45
    assert scored[BUYER][1]["slot_offset"] == 0


def test_freshness_infrastructure_prior():
    scored = score_team_membership(
        [_buyer(BUYER)], [_holder(BUYER, 10.0)], frozenset(),
        first_seen={BUYER: 1000}, sig_count={BUYER: 3}, graduated_at=1000 + 3600,
    )
    # young (<24h → 1.0) + narrow (<10 sigs → 1.0) → E_fresh 1.0 → +0.05
    assert scored[BUYER][0] == 0.40


def test_build_cluster_persists_scored_and_recovers_exited_member():
    holders = [_holder(HOLDER, 8.0)]
    buyers = [_buyer(HOLDER), _buyer(EXITED)]
    tc, scored = build_team_cluster_post_grad(
        MINT, buyers, holders, frozenset(),
        entity_edges={EXITED: {"funder"}, HOLDER: {"funder"}},
        funder_by_wallet={EXITED: FUNDER, HOLDER: FUNDER},
        graduated_at=5000,
    )
    assert tc is not None
    # HOLDER (buyer∩holder + funder edge) is a member; EXITED recovered via
    # funder entity + shared-funder + early buy
    assert HOLDER in tc.member_addresses
    assert EXITED in scored
