"""Phase B: slot/block-index resolution, Jito-adjacency, per-slot cache, features."""

import pytest

from src.analyzer.microstructure import resolve_microstructure, _compute_features, MicroRow
from src.analyzer.coordination import edges_behavioral
from src.ingest.helius import Swap

W1 = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
W2 = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"
W3 = "7s1da8DduuBFqGra5bJBjpnvL5E9mGzCuMk1Qkh4or2Z"
MINT = "M" * 44


def _buy(wallet, ts, sig):
    return Swap(side="buy", token_mint=MINT, sol_amount=1.0, token_amount=100.0,
               signer=wallet, timestamp=ts, slot=ts, tx_signature=sig)


class _StubRpc:
    """Serves canned slots per signature and ordered signatures per slot."""

    def __init__(self, slot_by_sig, block_sigs):
        self._slot_by_sig = slot_by_sig
        self._block_sigs = block_sigs
        self.tx_calls = 0
        self.block_calls = 0

    async def get_transaction(self, sig):
        self.tx_calls += 1
        slot = self._slot_by_sig.get(sig)
        return {"slot": slot} if slot is not None else None

    async def get_block_signatures(self, slot):
        self.block_calls += 1
        return self._block_sigs.get(slot)


@pytest.mark.asyncio
async def test_resolve_slots_and_bundle_adjacency():
    # 3 buys: two in slot 100 at adjacent block indices (bundle), one in slot 102
    buys = [_buy(W1, 1000, "sigA"), _buy(W2, 1001, "sigB"), _buy(W3, 1005, "sigC")]
    stub = _StubRpc(
        slot_by_sig={"sigA": 100, "sigB": 100, "sigC": 102},
        block_sigs={
            100: ["other", "sigA", "sigB", "more"],   # sigA idx 1, sigB idx 2 → adjacent
            102: ["sigC", "x"],
        },
    )
    rows, feats, slot_map = await resolve_microstructure(stub, MINT, buys, first_n=50)

    assert slot_map == {"sigA": 100, "sigB": 100, "sigC": 102}
    assert stub.block_calls == 2                     # one per distinct slot, cached
    by_sig = {r.tx_signature: r for r in rows}
    assert by_sig["sigA"].block_index == 1
    assert by_sig["sigB"].block_index == 2
    assert by_sig["sigA"].is_bundled == 1 and by_sig["sigB"].is_bundled == 1
    assert by_sig["sigC"].is_bundled == 0            # alone in its slot
    assert by_sig["sigC"].slot_offset_from_first == 2
    assert feats.launch_slot_snipe_count == 2        # both in first slot (100)
    assert feats.buys_first_slot == 2
    assert feats.max_same_slot_group == 2
    assert feats.bundled_adjacent_count == 2


@pytest.mark.asyncio
async def test_non_adjacent_same_slot_not_bundled():
    buys = [_buy(W1, 1000, "s1"), _buy(W2, 1001, "s2")]
    stub = _StubRpc(
        slot_by_sig={"s1": 50, "s2": 50},
        block_sigs={50: ["s1", "gap1", "gap2", "s2"]},   # indices 0 and 3 → not adjacent
    )
    rows, feats, _ = await resolve_microstructure(stub, MINT, buys, first_n=50)
    assert all(r.is_bundled == 0 for r in rows)
    assert feats.max_same_slot_group == 2
    assert feats.bundled_adjacent_count == 0


@pytest.mark.asyncio
async def test_pruned_block_degrades_gracefully():
    buys = [_buy(W1, 1000, "s1")]
    stub = _StubRpc(slot_by_sig={"s1": 9}, block_sigs={})   # getBlock returns None
    rows, feats, slot_map = await resolve_microstructure(stub, MINT, buys, first_n=50)
    assert slot_map == {"s1": 9}          # slot still known from getTransaction
    assert rows[0].block_index is None
    assert rows[0].is_bundled == 0


@pytest.mark.asyncio
async def test_empty_when_no_signatures():
    buys = [Swap(side="buy", token_mint=MINT, sol_amount=1.0, token_amount=1.0,
                 signer=W1, timestamp=1, slot=1, tx_signature=None)]
    stub = _StubRpc({}, {})
    rows, feats, slot_map = await resolve_microstructure(stub, MINT, buys, first_n=50)
    assert rows == [] and slot_map == {} and stub.tx_calls == 0


def test_edges_behavioral_cosine():
    # identical direction → linked; orthogonal → not; degenerate → skipped
    v = {
        W1: (1.0, 0.0, 0.5, 0.2),
        W2: (0.98, 0.0, 0.49, 0.2),   # ~same direction as W1
        W3: (0.0, 1.0, 0.0, 0.0),     # orthogonal
    }
    edges = edges_behavioral(v, threshold=0.92)
    pair = (W1, W2) if W1 < W2 else (W2, W1)
    assert pair in edges
    assert not any(W3 in e for e in edges)
    # near-zero vector excluded
    assert edges_behavioral({W1: (0.01, 0.0), W2: (0.01, 0.0)}) == set()
