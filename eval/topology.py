"""Phase 4 — graph topology features from the per-launch wallet graph.

The hand-built noisy-OR over fixed edge weights (funder 0.90, same_slot 0.70, …)
is brittle and gameable. The literature says the discriminative structure is
TOPOLOGICAL, so we rebuild the launch graph and measure its shape:

  - star vs cluster  — one hub funding/co-buying with many satellites (a single
    operator batching) versus a professionalised mesh with division of labour.
    Captured by degree centralization (Freeman) and max/avg degree ratio.
  - average degree + clustering coefficient — rug networks show HIGHER average
    degree but LOWER clustering coefficient than sustainable ones (ACM WebSci '25).
  - community structure — Louvain communities instead of thresholding one edge at
    a time: n_communities, modularity, largest-community share.

STRICT point-in-time: every edge is rebuilt only from data known at graduation —
same-slot/same-block co-buys (bc_microstructure, first 50 buys), shared non-CEX
funder (wallet_funding, traced at graduation), and near-identical buy size
(bc_accumulation). Nothing post-graduation touches this.

    uv run python -m eval.topology            # compute + cache into launch_topology
    uv run python -m eval.topology --rebuild  # force recompute
"""

from __future__ import annotations

import sys
import time
from itertools import combinations

import networkx as nx

from src.common.db import get_connection

BUY_SIZE_REL_TOL = 0.02      # near-identical buy size ⇒ likely one operator
MAX_NODES = 300              # guard: pathological launches (O(k²) edge build)

FEATURES = [
    "topo_nodes", "topo_edges", "topo_density", "topo_avg_degree",
    "topo_max_degree_ratio", "topo_centralization", "topo_clustering",
    "topo_components", "topo_largest_comp_share",
    "topo_communities", "topo_modularity", "topo_largest_comm_share",
    "topo_same_slot_edges", "topo_funder_edges", "topo_size_edges",
]


def build_graph(conn, mint: str) -> tuple[nx.Graph, dict[str, int]]:
    """Rebuild the launch wallet-graph from point-in-time sources."""
    G = nx.Graph()
    counts = {"same_slot": 0, "funder": 0, "size": 0}

    buyers = [dict(r) for r in conn.execute(
        "SELECT wallet_address, total_sol_in FROM bc_accumulation WHERE token_mint = ?",
        (mint,))]
    if len(buyers) < 3:
        return G, counts
    buyers = buyers[:MAX_NODES]
    G.add_nodes_from(b["wallet_address"] for b in buyers)
    node_set = set(G.nodes)

    # 1. same-slot / same-block co-buys (strongest, cryptographic)
    by_slot: dict[int, list[str]] = {}
    for r in conn.execute(
        """SELECT wallet, slot FROM bc_microstructure
           WHERE token_mint = ? AND slot IS NOT NULL""", (mint,)):
        if r["wallet"] in node_set:
            by_slot.setdefault(int(r["slot"]), []).append(r["wallet"])
    for wallets in by_slot.values():
        uniq = sorted(set(wallets))
        if 2 <= len(uniq) <= 40:                      # a 100-wallet "slot" is noise
            for a, b in combinations(uniq, 2):
                G.add_edge(a, b); counts["same_slot"] += 1

    # 2. shared non-CEX funder
    by_funder: dict[str, list[str]] = {}
    ph = ",".join("?" * len(node_set))
    for r in conn.execute(
        f"""SELECT wallet, funder FROM wallet_funding
            WHERE hop = 1 AND funder IS NOT NULL AND funder != 'cex'
              AND wallet IN ({ph})""", tuple(node_set)):
        by_funder.setdefault(r["funder"], []).append(r["wallet"])
    for wallets in by_funder.values():
        uniq = sorted(set(wallets))
        if 2 <= len(uniq) <= 40:
            for a, b in combinations(uniq, 2):
                G.add_edge(a, b); counts["funder"] += 1

    # 3. near-identical buy size
    sized = sorted(((float(b["total_sol_in"] or 0), b["wallet_address"])
                    for b in buyers if (b["total_sol_in"] or 0) > 0))
    for i in range(len(sized)):
        ai, wi = sized[i]
        for j in range(i + 1, len(sized)):
            aj, wj = sized[j]
            if aj - ai > ai * BUY_SIZE_REL_TOL:
                break
            G.add_edge(wi, wj); counts["size"] += 1

    return G, counts


def topology_features(G: nx.Graph, counts: dict[str, int]) -> dict:
    n = G.number_of_nodes()
    m = G.number_of_edges()
    f = {k: None for k in FEATURES}
    f["topo_nodes"], f["topo_edges"] = n, m
    f["topo_same_slot_edges"] = counts["same_slot"]
    f["topo_funder_edges"] = counts["funder"]
    f["topo_size_edges"] = counts["size"]
    if n < 3:
        return f

    degs = [d for _, d in G.degree()]
    avg_deg = sum(degs) / n
    max_deg = max(degs)
    f["topo_density"] = nx.density(G)
    f["topo_avg_degree"] = avg_deg
    f["topo_max_degree_ratio"] = (max_deg / avg_deg) if avg_deg > 0 else 0.0
    # Freeman degree centralization: 1.0 = perfect star, 0.0 = regular mesh
    denom = (n - 1) * (n - 2)
    f["topo_centralization"] = (sum(max_deg - d for d in degs) / denom) if denom > 0 else 0.0
    f["topo_clustering"] = nx.average_clustering(G)

    comps = list(nx.connected_components(G))
    f["topo_components"] = len(comps)
    f["topo_largest_comp_share"] = max((len(c) for c in comps), default=0) / n

    if m > 0:
        try:
            comms = nx.community.louvain_communities(G, seed=0)
            f["topo_communities"] = len(comms)
            f["topo_modularity"] = nx.community.modularity(G, comms)
            f["topo_largest_comm_share"] = max((len(c) for c in comms), default=0) / n
        except Exception:
            pass
    return f


def ensure_table(conn) -> None:
    cols = ", ".join(f"{k} REAL" for k in FEATURES)
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS launch_topology (
                token_mint TEXT PRIMARY KEY,
                computed_at INTEGER NOT NULL,
                {cols}
            )""")
    conn.commit()


def compute_all(rebuild: bool = False) -> int:
    conn = get_connection()
    try:
        ensure_table(conn)
        done = set() if rebuild else {
            r[0] for r in conn.execute("SELECT token_mint FROM launch_topology")}
        mints = [r[0] for r in conn.execute(
            """SELECT DISTINCT ba.token_mint FROM bc_accumulation ba
               JOIN graduation_events ge ON ge.token_mint = ba.token_mint
               WHERE ge.pipeline_version >= 2""")]
        todo = [m for m in mints if m not in done]
        print(f"{len(mints)} v2 launches · {len(todo)} to compute")

        rows, now = [], int(time.time())
        for i, mint in enumerate(todo, 1):
            G, counts = build_graph(conn, mint)
            f = topology_features(G, counts)
            rows.append((mint, now, *[f[k] for k in FEATURES]))
            if i % 500 == 0:
                print(f"  {i}/{len(todo)}…")
        if rows:
            ph = ",".join("?" * (len(FEATURES) + 2))
            conn.executemany(
                f"""INSERT OR REPLACE INTO launch_topology
                    (token_mint, computed_at, {','.join(FEATURES)}) VALUES ({ph})""",
                rows)
            conn.commit()
        print(f"wrote {len(rows)} topology rows")
        return len(rows)
    finally:
        conn.close()


def load_topology(conn) -> dict[str, dict]:
    """token_mint → topology feature dict (for merging into the model harness)."""
    try:
        return {r["token_mint"]: {k: r[k] for k in FEATURES}
                for r in conn.execute(f"SELECT token_mint, {','.join(FEATURES)} FROM launch_topology")}
    except Exception:
        return {}


if __name__ == "__main__":
    compute_all(rebuild="--rebuild" in sys.argv)
