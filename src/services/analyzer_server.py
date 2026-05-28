"""FastAPI server — main entry point for the analyzer service."""

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.common.config import settings
from src.common.db import get_connection, migrate


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()
    yield


app = FastAPI(title="Solana Co-pilot", version="0.1.0", lifespan=lifespan)

templates = Jinja2Templates(directory="src/ui/templates")
app.mount("/static", StaticFiles(directory="src/ui/static"), name="static")


class AnalyzeRequest(BaseModel):
    mint: str


class AnalyzeResponse(BaseModel):
    mint: str
    summary: str
    bundle_pct: float | None
    dev_pct: float | None
    top10_pct: float | None
    smart_money_count: int
    matched_narratives: list[str]
    lp_burned: bool
    raw: dict[str, Any]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Serve the main web UI."""
    return templates.TemplateResponse(request, "index.html")


@app.post("/analyze")
async def analyze_token(request: Request) -> Any:
    """Run the full analysis pipeline for a token mint address.

    Accepts both JSON (Content-Type: application/json) and form data (HTMX).
    Returns an HTML fragment for HTMX requests, JSON otherwise.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        mint = (body.get("mint") or "").strip()
    else:
        form = await request.form()
        mint = (form.get("mint") or "").strip()

    if not mint:
        raise HTTPException(status_code=422, detail="mint is required")

    result_data = await _run_pipeline(mint)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "analysis.html", result_data)

    return AnalyzeResponse(**result_data)


async def _run_pipeline(mint: str) -> dict[str, Any]:
    """Execute the full analysis pipeline for a mint address."""
    from src.analyzer.narrative_match import get_active_narratives, match_token_to_narratives
    from src.analyzer.smart_money import find_smart_money_in_buyers, get_smart_money_wallets
    from src.analyzer.summarize import SmartMoneyEntry, TokenAnalysis, summarize
    from src.analyzer.team_detect import compute_dev_pct, identify_team_cluster
    from src.analyzer.wallet_cluster import build_clusters, compute_bundle_pct
    from src.ingest.gmgn import GMGNClient, parse_token_info
    from src.ingest.helius import HeliusClient, decode_swap_transaction

    conn = get_connection()
    try:
        # 1. Token info from GMGN
        async with GMGNClient() as gmgn:
            token_raw = await gmgn.get_token_info(mint)
        token = parse_token_info(mint, token_raw)

        # 2. Buyer swaps via Helius
        async with HeliusClient() as helius:
            txs = await helius.get_transactions_for_address(mint, limit=200)
            buyers = [b for tx in txs if (b := decode_swap_transaction(tx)) is not None]

            # 3. Cluster buyers by funding source
            clusters = await build_clusters(buyers, token.created_at, helius)

        bundle_pct = compute_bundle_pct(clusters, buyers)
        token.bundle_pct = bundle_pct

        smart_money_list = get_smart_money_wallets(conn)
        smart_money_buyers = find_smart_money_in_buyers(buyers, smart_money_list)
        team_cluster = identify_team_cluster(token, clusters, smart_money_list)

        dev_pct = compute_dev_pct(team_cluster, buyers) if team_cluster else 0.0
        token.dev_pct = dev_pct

        # 4. Narratives
        active_narratives = get_active_narratives(conn)
        matched_narratives = match_token_to_narratives(token, active_narratives)

        # 5. Smart money entries with MC at entry (simplified — use snapshot)
        sm_entries = [SmartMoneyEntry(wallet=w) for w in smart_money_buyers]

        # 6. Summarize
        analysis = TokenAnalysis(
            token=token,
            team_cluster=team_cluster,
            smart_money_entries=sm_entries,
            matched_narratives=matched_narratives,
            narrative_states=active_narratives,
            past_deployments=[],
            raw_stats={"bundle_pct": bundle_pct, "dev_pct": dev_pct},
        )
        result = await summarize(analysis, provider=settings.llm_provider)

        # 7. Persist token
        conn.execute(
            """INSERT OR REPLACE INTO tokens
               (mint, symbol, name, launchpad, created_at, market_cap_usd_snapshot,
                holders_count_snapshot, lp_burned, top10_pct, bundle_pct, dev_pct,
                narrative_tags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                token.mint, token.symbol, token.name, token.launchpad, token.created_at,
                token.market_cap_usd_snapshot, token.holders_count_snapshot,
                int(token.lp_burned), token.top10_pct, token.bundle_pct, token.dev_pct,
                json.dumps(matched_narratives),
            ),
        )
        conn.commit()

    finally:
        conn.close()

    return {
        "token": token,
        "mint": mint,
        "summary": result.text,
        "bundle_pct": bundle_pct,
        "dev_pct": dev_pct,
        "top10_pct": token.top10_pct,
        "smart_money_count": len(sm_entries),
        "matched_narratives": matched_narratives,
        "lp_burned": token.lp_burned,
        "raw": {
            "confidence": result.metadata.confidence,
            "signals": result.metadata.top_signals,
            "suggested_position_pct": result.metadata.suggested_position_pct,
        },
    }


@app.get("/token/{mint}")
async def get_token(mint: str) -> dict[str, Any]:
    """Return cached analysis data for a previously analysed token."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM tokens WHERE mint = ?", (mint,)).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="token not found")

    data = dict(row)
    data["narrative_tags"] = json.loads(data.get("narrative_tags") or "[]")
    data["lp_burned"] = bool(data.get("lp_burned"))
    return data


@app.get("/narratives")
async def list_narratives(request: Request) -> Any:
    """Return active narratives ordered by velocity.

    Returns HTML fragment for HTMX requests, JSON otherwise.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, label, keywords, current_velocity, status
               FROM narratives
               WHERE status IN ('emerging', 'hot')
               ORDER BY current_velocity DESC"""
        ).fetchall()
    finally:
        conn.close()

    narratives = [
        {
            "id": r["id"],
            "label": r["label"],
            "keywords": json.loads(r["keywords"]) if isinstance(r["keywords"], str) else r["keywords"],
            "current_velocity": r["current_velocity"],
            "status": r["status"],
        }
        for r in rows
    ]

    if request.headers.get("HX-Request"):
        items = "".join(
            f'<span class="badge narrative" title="{n["current_velocity"]:.0f}/h">'
            f'{n["label"]}</span>'
            for n in narratives
        ) or "<em>No active narratives</em>"
        return HTMLResponse(items)

    return narratives


@app.get("/smart-money")
async def list_smart_money() -> list[dict[str, Any]]:
    """Return wallets with smart_money_score >= 0.7."""
    from src.analyzer.smart_money import get_smart_money_wallets

    conn = get_connection()
    try:
        wallets = get_smart_money_wallets(conn)
    finally:
        conn.close()

    return [
        {
            "address": w.address,
            "label": w.label,
            "smart_money_score": w.smart_money_score,
            "win_rate_90d": w.win_rate_90d,
            "total_trades": w.total_trades,
        }
        for w in wallets
    ]


@app.get("/journal")
async def list_my_trades() -> list[dict[str, Any]]:
    """Return my trade log, most recent first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM my_trades ORDER BY ts DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        trade = dict(row)
        trade["rules_followed"] = json.loads(trade.get("rules_followed") or "[]")
        trade["lp_burned"] = bool(trade.get("lp_burned"))
        result.append(trade)
    return result


@app.post("/journal")
async def log_trade(request: Request) -> dict[str, Any]:
    """Manually log a trade from the UI conviction form."""
    from src.services.journal import save_trade
    import time

    form = await request.form()
    mint = (form.get("mint") or "").strip()
    if not mint:
        raise HTTPException(status_code=422, detail="mint is required")

    from src.common.models import Trade
    trade = Trade(
        tx_signature=f"manual_{mint}_{int(time.time())}",
        token_mint=mint,
        side=str(form.get("side") or "buy"),
        ts=int(time.time()),
        sol_amount=0.0,
        tokens=0.0,
        price_sol=0.0,
        conviction=int(form.get("conviction") or 3),
        notes=str(form.get("notes") or "") or None,
        source_tag="manual",
    )

    conn = get_connection()
    try:
        save_trade(trade, conn)
    finally:
        conn.close()

    return {"status": "logged", "tx_signature": trade.tx_signature}


@app.get("/backtest")
async def run_backtest() -> list[dict[str, Any]]:
    """Run backtest against all logged trades and return per-rule stats."""
    from src.strategy.backtest import run_backtest as _run_backtest
    from src.strategy.rules import ENTRY_RULES

    conn = get_connection()
    try:
        results = _run_backtest(ENTRY_RULES, conn)
    finally:
        conn.close()

    return [
        {
            "rule_id": r.rule_id,
            "trades_triggered": r.trades_triggered,
            "win_rate": r.win_rate,
            "avg_pnl_sol": r.avg_pnl_sol,
            "total_pnl_sol": r.total_pnl_sol,
        }
        for r in results
    ]
