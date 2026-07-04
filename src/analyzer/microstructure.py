"""Sub-second trade microstructure — the finest behavioral resolution on Solana.

Solana has no true milliseconds: time is discrete ~400ms slots. The finest
physical ordering is (slot, intra-block transaction index). That ordering is
exactly what separates coordinated insiders from organic buyers:

  - launch-slot snipe  : buying in the very first slot of the curve — near-
                         certain insider (no human reacts that fast)
  - same-block bundle  : ≥2 of our buys in one slot at ADJACENT block indices
                         = atomic/Jito bundle fingerprint
  - slot-reaction lag  : how many slots after the first buy a wallet acted

We resolve the first N buys of a graduation by fetching each tx's slot
(getTransaction) and each distinct block's ordered signatures (getBlock, cached
per coin). Free RPC; zero Solana Tracker cost. The tape already carries the
tx signatures (Swap.tx_signature from solana_tracker._trade_to_swap).
"""

import logging
import time
from dataclasses import dataclass

from src.ingest.helius import Swap

logger = logging.getLogger(__name__)


@dataclass
class MicroRow:
    token_mint: str
    wallet: str
    tx_signature: str
    slot: int | None
    block_index: int | None
    slot_offset_from_first: int | None
    same_slot_rank: int | None
    same_slot_group_size: int | None
    is_bundled: int


@dataclass
class MicroFeatures:
    launch_slot_snipe_count: int = 0        # our buys in the very first slot
    buys_first_slot: int = 0
    buys_first_3_slots: int = 0
    distinct_slots_first_20_buys: int = 0
    max_same_slot_group: int = 0
    bundled_adjacent_count: int = 0         # buys flagged as same-slot adjacent (Jito)


async def resolve_microstructure(
    rpc, token_mint: str, buys: list[Swap], first_n: int,
) -> tuple[list[MicroRow], MicroFeatures, dict[str, int]]:
    """Resolve (slot, block_index) for the first `first_n` buys.

    buys must be the BUY swaps in ascending timestamp order. Returns
    (per-tx rows, per-coin features, {tx_signature: real_slot}) — the last is
    used by the caller to remap Swap.slot before coordination.
    """
    target = [s for s in buys if s.tx_signature][:first_n]
    if not target:
        return [], MicroFeatures(), {}

    # tx → slot (one call per unique signature)
    slot_by_sig: dict[str, int] = {}
    for s in target:
        sig = s.tx_signature
        if sig in slot_by_sig:
            continue
        try:
            tx = await rpc.get_transaction(sig)
        except Exception:
            tx = None
        if tx and isinstance(tx.get("slot"), int):
            slot_by_sig[sig] = tx["slot"]

    if not slot_by_sig:
        return [], MicroFeatures(), {}

    # slot → ordered signatures (one call per distinct slot, cached)
    block_order: dict[int, dict[str, int]] = {}
    for slot in sorted(set(slot_by_sig.values())):
        try:
            sigs = await rpc.get_block_signatures(slot)
        except Exception:
            sigs = None
        if sigs:
            block_order[slot] = {sig: i for i, sig in enumerate(sigs)}

    first_slot = min(slot_by_sig.values())
    # group our target txs by slot, preserving intra-block order
    by_slot: dict[int, list[Swap]] = {}
    for s in target:
        slot = slot_by_sig.get(s.tx_signature)
        if slot is not None:
            by_slot.setdefault(slot, []).append(s)

    rows: list[MicroRow] = []
    for slot, group in by_slot.items():
        order = block_order.get(slot, {})
        # order our group by real intra-block index when known, else by timestamp
        group.sort(key=lambda s: (order.get(s.tx_signature, 1 << 30), s.timestamp))
        indices = sorted(order.get(s.tx_signature) for s in group
                         if order.get(s.tx_signature) is not None)
        # adjacent block indices among our buys in this slot = atomic bundle
        adjacent = {
            indices[i] for i in range(len(indices) - 1)
            if indices[i + 1] - indices[i] == 1
        } | {
            indices[i + 1] for i in range(len(indices) - 1)
            if indices[i + 1] - indices[i] == 1
        }
        for rank, s in enumerate(group):
            bi = order.get(s.tx_signature)
            rows.append(MicroRow(
                token_mint=token_mint,
                wallet=s.signer,
                tx_signature=s.tx_signature,
                slot=slot,
                block_index=bi,
                slot_offset_from_first=slot - first_slot,
                same_slot_rank=rank,
                same_slot_group_size=len(group),
                is_bundled=int(bi in adjacent) if bi is not None else 0,
            ))

    feats = _compute_features(rows, first_slot)
    return rows, feats, dict(slot_by_sig)


def _compute_features(rows: list[MicroRow], first_slot: int) -> MicroFeatures:
    if not rows:
        return MicroFeatures()
    slots = [r.slot for r in rows if r.slot is not None]
    first3 = {s for s in sorted(set(slots))[:3]}
    per_slot_count: dict[int, int] = {}
    for s in slots:
        per_slot_count[s] = per_slot_count.get(s, 0) + 1
    return MicroFeatures(
        launch_slot_snipe_count=sum(1 for r in rows if r.slot == first_slot),
        buys_first_slot=per_slot_count.get(first_slot, 0),
        buys_first_3_slots=sum(1 for s in slots if s in first3),
        distinct_slots_first_20_buys=len({s for s in slots[:20]}),
        max_same_slot_group=max(per_slot_count.values(), default=0),
        bundled_adjacent_count=sum(1 for r in rows if r.is_bundled),
    )


def upsert_microstructure(conn, rows: list[MicroRow]) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT INTO bc_microstructure
               (token_mint, wallet, tx_signature, slot, block_index,
                slot_offset_from_first, same_slot_rank, same_slot_group_size,
                is_bundled, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, tx_signature) DO UPDATE SET
               slot=excluded.slot, block_index=excluded.block_index,
               slot_offset_from_first=excluded.slot_offset_from_first,
               same_slot_rank=excluded.same_slot_rank,
               same_slot_group_size=excluded.same_slot_group_size,
               is_bundled=excluded.is_bundled, resolved_at=excluded.resolved_at""",
        [
            (r.token_mint, r.wallet, r.tx_signature, r.slot, r.block_index,
             r.slot_offset_from_first, r.same_slot_rank, r.same_slot_group_size,
             r.is_bundled, int(time.time()))
            for r in rows
        ],
    )
    conn.commit()


def upsert_micro_features(conn, token_mint: str, f: MicroFeatures) -> None:
    """Merge microstructure feature columns into the mint's bc_flow_features row."""
    conn.execute(
        """UPDATE bc_flow_features SET
               launch_slot_snipe_count = ?, buys_first_slot = ?,
               buys_first_3_slots = ?, distinct_slots_first_20_buys = ?,
               max_same_slot_group = ?, bundled_adjacent_count = ?
           WHERE token_mint = ?""",
        (
            f.launch_slot_snipe_count, f.buys_first_slot, f.buys_first_3_slots,
            f.distinct_slots_first_20_buys, f.max_same_slot_group,
            f.bundled_adjacent_count, token_mint,
        ),
    )
    conn.commit()
