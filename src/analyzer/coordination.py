"""Coordinated-entity detection — identify teams operating a coin by BEHAVIOR.

Rug teams use fresh wallets every launch, so address identity fails. Instead we
detect that a SET of wallets is ONE coordinated entity via signals that survive
wallet rotation (strongest first):

  1. same-slot / bundle co-occurrence  — atomic, cryptographic, zero API cost
  2. shared funder                      — fresh wallets still need SOL from somewhere
  3. identical buy sizes                — coordination fingerprint
  4. lockstep selling                   — distinct wallets dumping together
  5. whole-cluster fresh-wallet age     — synchronized creation

All functions here are PURE (take Swap lists, return values — no IO). Two drivers
call analyze_coin: a batch pass over live_trades and the live_watcher's rolling state.
"""

import hashlib
import json
from dataclasses import dataclass, field

from src.ingest.helius import Swap, CEX_HOT_WALLETS

# Fresh-wallet thresholds (from research)
_FRESH_CRITICAL_S = 24 * 3600
_FRESH_WARNING_S = 3 * 24 * 3600
_SNIPER_OFFSET_S = 120

# State classification (entity net flow)
_DISTRIBUTING_SOLD_FRAC = 0.30   # sold > 30% of accumulated tokens
_DUMPED_SOLD_FRAC = 0.90


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class Bundle:
    slot: int
    wallets: tuple[str, ...]
    token_amount: float
    sol_amount: float


@dataclass
class BundleStats:
    bundled_supply_pct: float
    bundle_wallet_count: int
    largest_bundle_size: int
    bundle_count: int


@dataclass
class Entity:
    entity_id: str
    wallets: tuple[str, ...]
    supply_pct: float
    wallet_count: int
    fresh_ratio: float
    state: str                       # ACCUMULATING|HOLDING|DISTRIBUTING|DUMPED
    edge_sources: tuple[str, ...]


@dataclass
class CoinCoordination:
    token_mint: str
    entity_count: int
    largest_entity_supply_pct: float
    largest_entity_wallet_count: int
    largest_entity_fresh_ratio: float
    largest_entity_state: str | None
    bundle_stats: BundleStats
    entities: list[Entity] = field(default_factory=list)


# ── helpers ────────────────────────────────────────────────────────────────────

def _dedup(swaps: list[Swap]) -> list[Swap]:
    seen: set[tuple[str, str, int, str]] = set()
    out: list[Swap] = []
    for s in swaps:
        k = (s.token_mint, s.signer, s.slot, s.side)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


# ── 1a. same-slot bundles ───────────────────────────────────────────────────────

def group_by_slot(swaps: list[Swap], slot_window: int = 0) -> list[Bundle]:
    """Group BUY swaps whose slots are within slot_window into bundles.

    A bundle requires >=2 DISTINCT wallets sharing a slot-group. slot_window=0 means
    exactly the same slot (strongest); 1 also catches adjacent-slot Jito landings.
    """
    buys = sorted(
        (s for s in _dedup(swaps) if s.side == "buy"),
        key=lambda s: s.slot,
    )
    bundles: list[Bundle] = []
    i = 0
    n = len(buys)
    while i < n:
        start_slot = buys[i].slot
        group: list[Swap] = []
        j = i
        while j < n and buys[j].slot - start_slot <= slot_window:
            group.append(buys[j])
            j += 1
        wallets = {s.signer for s in group}
        if len(wallets) >= 2:
            bundles.append(Bundle(
                slot=start_slot,
                wallets=tuple(sorted(wallets)),
                token_amount=sum(s.token_amount for s in group),
                sol_amount=sum(s.sol_amount for s in group),
            ))
        i = j
    return bundles


def compute_bundle_stats(
    swaps: list[Swap],
    total_supply: float | None = None,
    slot_window: int = 0,
) -> BundleStats:
    """Bundle metrics. Denominator = total_supply if given, else observed buy volume."""
    bundles = group_by_slot(swaps, slot_window)
    bundled_tokens = sum(b.token_amount for b in bundles)
    denom = total_supply if (total_supply and total_supply > 0) else sum(
        s.token_amount for s in _dedup(swaps) if s.side == "buy"
    )
    pct = round(bundled_tokens / denom * 100, 2) if denom > 0 else 0.0
    wallets: set[str] = set()
    for b in bundles:
        wallets.update(b.wallets)
    return BundleStats(
        bundled_supply_pct=min(pct, 100.0),
        bundle_wallet_count=len(wallets),
        largest_bundle_size=max((len(b.wallets) for b in bundles), default=0),
        bundle_count=len(bundles),
    )


# ── 1b. edge sources (each → set of canonical wallet pairs) ──────────────────────

def edges_same_slot(swaps: list[Swap], slot_window: int = 0) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for b in group_by_slot(swaps, slot_window):
        w = b.wallets
        for x in range(len(w)):
            for y in range(x + 1, len(w)):
                edges.add(_pair(w[x], w[y]))
    return edges


def edges_buy_size_fingerprint(swaps: list[Swap], rel_tol: float = 0.02) -> set[tuple[str, str]]:
    """Link wallets whose largest buy sizes (SOL) are within rel_tol of each other."""
    largest: dict[str, float] = {}
    for s in swaps:
        if s.side == "buy" and s.sol_amount > largest.get(s.signer, 0.0):
            largest[s.signer] = s.sol_amount
    items = [(w, amt) for w, amt in largest.items() if amt > 0]
    items.sort(key=lambda x: x[1])
    edges: set[tuple[str, str]] = set()
    for i in range(len(items)):
        wi, ai = items[i]
        for j in range(i + 1, len(items)):
            wj, aj = items[j]
            if aj - ai > ai * rel_tol:
                break
            edges.add(_pair(wi, wj))
    return edges


def edges_lockstep_sells(swaps: list[Swap], window_s: int = 2) -> set[tuple[str, str]]:
    """Link distinct wallets that sell within window_s seconds of each other."""
    sells = sorted((s for s in swaps if s.side == "sell"), key=lambda s: s.timestamp)
    edges: set[tuple[str, str]] = set()
    for i in range(len(sells)):
        for j in range(i + 1, len(sells)):
            if sells[j].timestamp - sells[i].timestamp > window_s:
                break
            if sells[i].signer != sells[j].signer:
                edges.add(_pair(sells[i].signer, sells[j].signer))
    return edges


def edges_shared_funder(funder_by_wallet: dict[str, str | None]) -> set[tuple[str, str]]:
    """Link wallets sharing a non-CEX funder. Takes a precomputed map (stays pure)."""
    by_funder: dict[str, list[str]] = {}
    for wallet, funder in funder_by_wallet.items():
        if not funder or funder == "cex" or funder in CEX_HOT_WALLETS:
            continue
        by_funder.setdefault(funder, []).append(wallet)
    edges: set[tuple[str, str]] = set()
    for wallets in by_funder.values():
        for i in range(len(wallets)):
            for j in range(i + 1, len(wallets)):
                edges.add(_pair(wallets[i], wallets[j]))
    return edges


# ── 1d. fresh-wallet scoring ────────────────────────────────────────────────────

def fresh_flags(
    wallet_first_seen: dict[str, int],
    wallet_first_buy_offset: dict[str, float],
    now_ts: int,
) -> dict[str, str]:
    """Per-wallet freshness flag: critical/warning/sniper/none."""
    flags: dict[str, str] = {}
    for wallet, first_seen in wallet_first_seen.items():
        age = now_ts - first_seen if first_seen else None
        offset = wallet_first_buy_offset.get(wallet)
        if age is not None and age < _FRESH_CRITICAL_S:
            flags[wallet] = "critical"
        elif age is not None and age < _FRESH_WARNING_S:
            flags[wallet] = "warning"
        elif offset is not None and offset <= _SNIPER_OFFSET_S:
            flags[wallet] = "sniper"
        else:
            flags[wallet] = "none"
    return flags


def fresh_ratio(wallets: tuple[str, ...], flags: dict[str, str]) -> float:
    if not wallets:
        return 0.0
    fresh = sum(1 for w in wallets if flags.get(w, "none") in ("critical", "warning", "sniper"))
    return round(fresh / len(wallets), 3)


# ── 1c. union-find entity assembly ──────────────────────────────────────────────

def _entity_id(wallets: tuple[str, ...]) -> str:
    h = hashlib.sha1("|".join(sorted(wallets)).encode()).hexdigest()
    return h[:16]


def _entity_state(member_swaps: list[Swap]) -> str:
    bought = sum(s.token_amount for s in member_swaps if s.side == "buy")
    sold = sum(s.token_amount for s in member_swaps if s.side == "sell")
    if bought <= 0:
        return "HOLDING"
    frac = sold / bought
    if frac >= _DUMPED_SOLD_FRAC:
        return "DUMPED"
    if frac >= _DISTRIBUTING_SOLD_FRAC:
        return "DISTRIBUTING"
    if sold == 0:
        return "ACCUMULATING"
    return "HOLDING"


def assemble_entities(
    swaps: list[Swap],
    edges: set[tuple[str, str]],
    fresh: dict[str, str] | None = None,
    total_supply: float | None = None,
    edge_labels: dict[tuple[str, str], set[str]] | None = None,
) -> list[Entity]:
    """Union-find over wallets joined by edges. Only multi-wallet components qualify."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    # group wallets by component root
    comps: dict[str, set[str]] = {}
    for w in list(parent.keys()):
        comps.setdefault(find(w), set()).add(w)

    fresh = fresh or {}
    denom = total_supply if (total_supply and total_supply > 0) else sum(
        s.token_amount for s in swaps if s.side == "buy"
    )

    entities: list[Entity] = []
    for members in comps.values():
        if len(members) < 2:
            continue
        member_swaps = [s for s in swaps if s.signer in members]
        bought = sum(s.token_amount for s in member_swaps if s.side == "buy")
        supply_pct = round(bought / denom * 100, 2) if denom > 0 else 0.0
        wallets = tuple(sorted(members))
        srcs: set[str] = set()
        if edge_labels:
            for a, b in edges:
                if a in members and b in members:
                    srcs |= edge_labels.get((a, b), set())
        entities.append(Entity(
            entity_id=_entity_id(wallets),
            wallets=wallets,
            supply_pct=min(supply_pct, 100.0),
            wallet_count=len(wallets),
            fresh_ratio=fresh_ratio(wallets, fresh),
            state=_entity_state(member_swaps),
            edge_sources=tuple(sorted(srcs)),
        ))
    entities.sort(key=lambda e: e.supply_pct, reverse=True)
    return entities


def analyze_coin(
    token_mint: str,
    swaps: list[Swap],
    total_supply: float | None = None,
    funder_by_wallet: dict[str, str | None] | None = None,
    fresh: dict[str, str] | None = None,
    *,
    slot_window: int = 0,
    size_tol: float = 0.005,   # near-exact only — buy-size over-links if too loose
    lockstep_s: int = 2,
) -> CoinCoordination:
    """Single entry point — both drivers call this. Pure."""
    swaps = _dedup(swaps)

    labeled: dict[tuple[str, str], set[str]] = {}
    def _add(es: set[tuple[str, str]], label: str) -> None:
        for e in es:
            labeled.setdefault(e, set()).add(label)

    _add(edges_same_slot(swaps, slot_window), "same_slot")
    _add(edges_buy_size_fingerprint(swaps, size_tol), "buy_size")
    _add(edges_lockstep_sells(swaps, lockstep_s), "lockstep_sell")
    if funder_by_wallet:
        _add(edges_shared_funder(funder_by_wallet), "funder")

    all_edges = set(labeled.keys())
    entities = assemble_entities(swaps, all_edges, fresh, total_supply, labeled)
    bundle_stats = compute_bundle_stats(swaps, total_supply, slot_window)

    largest = entities[0] if entities else None
    return CoinCoordination(
        token_mint=token_mint,
        entity_count=len(entities),
        largest_entity_supply_pct=largest.supply_pct if largest else 0.0,
        largest_entity_wallet_count=largest.wallet_count if largest else 0,
        largest_entity_fresh_ratio=largest.fresh_ratio if largest else 0.0,
        largest_entity_state=largest.state if largest else None,
        bundle_stats=bundle_stats,
        entities=entities,
    )


# ── persistence (IO) ─────────────────────────────────────────────────────────────

def upsert_coordination(conn, cc: CoinCoordination, source: str = "batch", phase: str = "launch") -> None:
    import time
    now = int(time.time())
    bs = cc.bundle_stats
    conn.execute(
        """INSERT INTO coin_coordination
               (token_mint, phase, computed_at, source, entity_count, bundled_supply_pct,
                bundle_wallet_count, largest_bundle_size, largest_entity_supply_pct,
                largest_entity_wallet_count, largest_entity_fresh_ratio, largest_entity_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, phase) DO UPDATE SET
               computed_at=excluded.computed_at, source=excluded.source,
               entity_count=excluded.entity_count, bundled_supply_pct=excluded.bundled_supply_pct,
               bundle_wallet_count=excluded.bundle_wallet_count,
               largest_bundle_size=excluded.largest_bundle_size,
               largest_entity_supply_pct=excluded.largest_entity_supply_pct,
               largest_entity_wallet_count=excluded.largest_entity_wallet_count,
               largest_entity_fresh_ratio=excluded.largest_entity_fresh_ratio,
               largest_entity_state=excluded.largest_entity_state""",
        (
            cc.token_mint, phase, now, source, cc.entity_count, bs.bundled_supply_pct,
            bs.bundle_wallet_count, bs.largest_bundle_size, cc.largest_entity_supply_pct,
            cc.largest_entity_wallet_count, cc.largest_entity_fresh_ratio, cc.largest_entity_state,
        ),
    )
    for e in cc.entities:
        conn.execute(
            """INSERT INTO coordinated_entities
                   (token_mint, phase, entity_id, member_addresses, wallet_count, supply_pct,
                    fresh_ratio, state, edge_sources, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_mint, phase, entity_id) DO UPDATE SET
                   member_addresses=excluded.member_addresses, wallet_count=excluded.wallet_count,
                   supply_pct=excluded.supply_pct, fresh_ratio=excluded.fresh_ratio,
                   state=excluded.state, edge_sources=excluded.edge_sources,
                   computed_at=excluded.computed_at""",
            (
                cc.token_mint, phase, e.entity_id, json.dumps(list(e.wallets)), e.wallet_count,
                e.supply_pct, e.fresh_ratio, e.state, json.dumps(list(e.edge_sources)), now,
            ),
        )
    conn.commit()
