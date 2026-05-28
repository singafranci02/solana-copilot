"""Pump.fun graduation monitor.

Detects when a token completes its bonding curve and migrates to PumpSwap.
Only tokens that graduate (~0.7-0.8% of all launches) receive structural
analysis — this is the primary quality filter.

Graduation = token raises ~85 SOL on Pump.fun bonding curve and is
automatically migrated to PumpSwap AMM (~$69K market cap at migration).

Detection strategy:
  Primary:  Pump.fun WebSocket event (event name assumed = 'migrate' — TODO: verify)
  Fallback: REST poll of pump.fun recently-graduated endpoint every 30 s

Post-detection pipeline (async, non-blocking):
  1. Record graduation_event to DB
  2. Fetch BC top-holders from Helius (who held at graduation?)
  3. Build team_cluster from BC-phase buyers + holder list
  4. Produce StructuralRead verdict
  5. Schedule distribution checks at +1h / +4h / +24h

Constants to verify against current deployment:
  PUMPSWAP_PROGRAM_ID — check Solscan for current PumpSwap AMM program
  GRADUATION_EVENT    — actual Socket.IO event name emitted at graduation
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from src.common.db import get_connection
from src.common.models import GraduationEvent, TeamCluster, TokenBuyer

logger = logging.getLogger(__name__)

# TODO: verify this program ID matches the live PumpSwap AMM deployment
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
PUMP_FUN_API = "https://frontend-api.pump.fun"  # REST fallback only

MIN_GRADUATION_MC_USD = 50_000.0   # sanity-check lower bound


# ── internal event struct ─────────────────────────────────────────────────────

@dataclass
class _MigrateEvent:
    mint: str
    pool_address: str | None
    event_ts: int


def _parse_migrate(raw: dict) -> _MigrateEvent | None:
    """Parse a raw Socket.IO migrate payload; field names are best-guess."""
    mint = raw.get("mint") or raw.get("token") or raw.get("address")
    if not mint:
        logger.debug("migrate event missing mint — keys: %s", list(raw.keys()))
        return None
    pool = raw.get("pool") or raw.get("pool_address") or raw.get("ammPool")
    ts = int(raw.get("timestamp") or time.time())
    return _MigrateEvent(mint=str(mint), pool_address=pool, event_ts=ts)


# ── monitor ───────────────────────────────────────────────────────────────────

class GraduationMonitor:
    """Connects to PumpPortal WebSocket and triggers structural analysis on graduation."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def run(self) -> None:
        """Connect and run forever; starts the REST poll fallback in background."""
        asyncio.create_task(self._poll_fallback())
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        PUMPPORTAL_WS,
                        heartbeat=30,
                        timeout=aiohttp.ClientTimeout(total=None, connect=15),
                    ) as ws:
                        logger.info("graduation monitor connected to PumpPortal WS")
                        await ws.send_json({"method": "subscribeMigration"})

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    raw = json.loads(msg.data)
                                    event = _parse_migrate(raw)
                                    if event and event.mint not in self._seen:
                                        self._seen.add(event.mint)
                                        lag = max(0, int(time.time()) - event.event_ts)
                                        logger.info(
                                            "graduation WS: %s (lag %ds)",
                                            event.mint[:8], lag,
                                        )
                                        asyncio.create_task(
                                            _handle_graduation(
                                                event.mint, event.pool_address, lag
                                            )
                                        )
                                except Exception:
                                    pass
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break

                        logger.warning("graduation WS closed — reconnecting")
            except Exception as exc:
                logger.warning("graduation WS error: %s — retrying", exc)
            await asyncio.sleep(5)

    async def _poll_fallback(self) -> None:
        """Poll REST endpoint every 30 s to catch graduations missed by WS."""
        url = f"{PUMP_FUN_API}/coins/recently-graduated"
        while True:
            await asyncio.sleep(30)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        coins = await resp.json()
                        for coin in (coins if isinstance(coins, list) else []):
                            mint = coin.get("mint")
                            if not mint or mint in self._seen:
                                continue
                            self._seen.add(mint)
                            pool = (
                                coin.get("raydiumPool")
                                or coin.get("pumpAmmPool")
                                or coin.get("pool")
                            )
                            raw_ts = int(coin.get("timestamp") or time.time())
                            lag = abs(int(time.time()) - raw_ts)
                            logger.info("graduation poll: %s (lag %ds)", mint[:8], lag)
                            asyncio.create_task(_handle_graduation(mint, pool, lag))
            except Exception as exc:
                logger.warning("graduation poll error: %s", exc)


# ── pipeline ──────────────────────────────────────────────────────────────────

async def _handle_graduation(
    mint: str,
    pool_address: str | None,
    detection_lag: int,
) -> None:
    """Persist the graduation and run full structural analysis."""
    from src.ingest.helius import HeliusClient

    conn = get_connection()
    try:
        # Ensure the token row exists (we may have missed the launch)
        token_row = conn.execute(
            "SELECT mint, symbol, created_at FROM tokens WHERE mint = ?", (mint,)
        ).fetchone()

        if token_row is None:
            async with HeliusClient() as helius:
                meta = await helius.get_token_metadata(mint)
            symbol = (meta.get("symbol") or "UNKNOWN") if meta else "UNKNOWN"
            name = (meta.get("name") or "Unknown") if meta else "Unknown"
            created_at = int(time.time())
            conn.execute(
                """INSERT OR IGNORE INTO tokens
                   (mint, symbol, name, launchpad, created_at, narrative_tags)
                   VALUES (?, ?, ?, 'pump.fun', ?, '[]')""",
                (mint, symbol, name, created_at),
            )
            conn.commit()
            from src.common import supabase_sync as sb
            asyncio.create_task(sb.token(mint, symbol, name, created_at))
        else:
            symbol = token_row["symbol"] or "?"

        now = int(time.time())
        conn.execute(
            """INSERT OR REPLACE INTO graduation_events
               (token_mint, graduated_at, detection_lag_seconds,
                pumpswap_pool_address, bc_top_holders_json)
               VALUES (?, ?, ?, ?, '[]')""",
            (mint, now, detection_lag, pool_address),
        )
        conn.commit()

        # Fetch top BC holders at graduation
        async with HeliusClient() as helius:
            accounts = await helius.get_token_largest_accounts(mint)
        bc_top_holders = _parse_bc_holders(accounts)

        conn.execute(
            "UPDATE graduation_events SET bc_top_holders_json = ? WHERE token_mint = ?",
            (json.dumps(bc_top_holders), mint),
        )
        conn.commit()

        # Load BC-phase buyers from our own token_buyers table
        rows = conn.execute(
            """SELECT wallet_address, sol_amount, tokens_received, bought_at
               FROM token_buyers WHERE token_mint = ?""",
            (mint,),
        ).fetchall()
        buyers = [
            TokenBuyer(
                token_mint=mint,
                wallet_address=r["wallet_address"],
                sol_amount=r["sol_amount"],
                tokens_received=r["tokens_received"],
                bought_at=r["bought_at"],
            )
            for r in rows
        ]

        grad_event = GraduationEvent(
            token_mint=mint,
            graduated_at=now,
            detection_lag_seconds=detection_lag,
            pumpswap_pool_address=pool_address,
            bc_top_holders=bc_top_holders,
        )

        logger.info(
            "structural analysis: $%s — %d BC buyers, %d top holders",
            symbol, len(buyers), len(bc_top_holders),
        )

        await analyse_graduation(grad_event, buyers, conn, symbol=symbol)

    except Exception:
        logger.exception("graduation analysis failed for %s", mint[:8])
    finally:
        conn.close()


async def analyse_graduation(
    event: GraduationEvent,
    buyers: list[TokenBuyer],
    conn,
    *,
    symbol: str = "?",
) -> None:
    """Full structural analysis pipeline for a graduated token."""
    from src.analyzer.team_detect import build_team_cluster_post_grad
    from src.analyzer.distribution import schedule_distribution_checks
    from src.analyzer.smart_money import (
        get_smart_money_wallets,
        find_smart_money_in_buyers,
        get_funder_reputation,
    )
    from src.strategy.rules import structural_read
    from src.common.cex_wallets import get_all_cex_addresses

    cex_addresses = get_all_cex_addresses(conn)

    team_cluster = build_team_cluster_post_grad(
        event.token_mint, buyers, event.bc_top_holders, cex_addresses
    )

    if team_cluster:
        conn.execute(
            """INSERT OR REPLACE INTO team_clusters
               (cluster_id, token_mint, funding_source, member_addresses,
                supply_pct_at_graduation, first_buy_offset_seconds, is_bc_sniper)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                team_cluster.cluster_id,
                team_cluster.token_mint,
                team_cluster.funding_source,
                json.dumps(team_cluster.member_addresses),
                team_cluster.supply_pct_at_graduation,
                team_cluster.first_buy_offset_seconds,
                int(team_cluster.is_bc_sniper),
            ),
        )
        conn.commit()

    smart_money_list = get_smart_money_wallets(conn)
    sm_buyers = find_smart_money_in_buyers(buyers, smart_money_list)

    funder_rep = None
    if team_cluster and team_cluster.funding_source:
        funder_rep = get_funder_reputation(team_cluster.funding_source, conn)

    from src.analyzer.team_memory import gather_memory_signals
    memory_signals = gather_memory_signals(team_cluster, conn)

    ctx = {
        "token_mint": event.token_mint,
        "team_cluster": team_cluster,
        "funder_rep": funder_rep,
        "smart_money_count": len(sm_buyers),
        "bc_top_holders": event.bc_top_holders,
        "distribution_signal": None,
        "bundle_pct": getattr(team_cluster, "supply_pct_at_graduation", 0.0) if team_cluster else 0.0,
        "memory_signals": memory_signals,
    }
    read = structural_read(ctx)

    # Persist verdict to SQLite so it's available for local queries
    conn.execute(
        """UPDATE graduation_events
           SET structural_verdict = ?, verdict_confidence = ?,
               smart_money_count = ?, dominant_factors_json = ?
           WHERE token_mint = ?""",
        (
            read.verdict, read.confidence,
            len(sm_buyers), json.dumps(read.dominant_factors),
            event.token_mint,
        ),
    )
    conn.commit()

    _print_graduation_alert(event, symbol, team_cluster, sm_buyers, funder_rep, read)

    # Sync to Supabase (fire-and-forget — never blocks analysis)
    from src.common import supabase_sync as sb
    asyncio.create_task(sb.graduation_event(
        token_mint=event.token_mint,
        graduated_at=event.graduated_at,
        detection_lag_seconds=event.detection_lag_seconds,
        structural_verdict=read.verdict,
        verdict_confidence=read.confidence,
        pumpswap_pool_address=event.pumpswap_pool_address,
        bc_top_holders_json=event.bc_top_holders,
    ))
    if team_cluster:
        asyncio.create_task(sb.team_cluster(
            cluster_id=team_cluster.cluster_id,
            token_mint=team_cluster.token_mint,
            funding_source=team_cluster.funding_source,
            member_addresses=team_cluster.member_addresses,
            supply_pct_at_graduation=team_cluster.supply_pct_at_graduation,
            first_buy_offset_seconds=team_cluster.first_buy_offset_seconds,
            is_bc_sniper=team_cluster.is_bc_sniper,
        ))

    # Close the learning loop — distribution checks update wallet_stats + funder_reputation
    await schedule_distribution_checks(event.token_mint, event.graduated_at)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_bc_holders(accounts: list[dict]) -> list[dict]:
    """Convert Helius largest-accounts response to ranked [{wallet, pct, ui_amount}]."""
    if not accounts:
        return []
    total = sum(float(a.get("uiAmount") or 0) for a in accounts)
    if total == 0:
        return []
    result = []
    for a in accounts[:20]:
        ui = float(a.get("uiAmount") or 0)
        if ui > 0:
            result.append({
                "wallet": a.get("address", ""),
                "pct": round(ui / total * 100, 2),
                "ui_amount": ui,
            })
    return result


def _print_graduation_alert(
    event: GraduationEvent,
    symbol: str,
    team_cluster: TeamCluster | None,
    sm_buyers: list,
    funder_rep,
    read,
) -> None:
    rugger_line = ""
    if funder_rep and funder_rep.is_known_rugger:
        n = len(funder_rep.graduated_mints)
        rate = funder_rep.rug_rate * 100
        rugger_line = f"\n  ⚠ KNOWN RUGGER — {n} prev graduates, rug rate {rate:.0f}%"

    if team_cluster:
        sniper_tag = " [BC sniper]" if team_cluster.is_bc_sniper else ""
        team_line = (
            f"team {len(team_cluster.member_addresses)} wallets, "
            f"{team_cluster.supply_pct_at_graduation:.1f}% supply{sniper_tag}"
        )
    else:
        team_line = "no team cluster detected"

    factors = " | ".join(read.dominant_factors) if read.dominant_factors else "no strong signals"

    logger.info(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  GRADUATED $%s — %s%s\n"
        "  %s\n"
        "  Smart money: %d  |  Lag: %ds\n"
        "  Verdict: %s  (confidence %.0f%%)\n"
        "  %s\n"
        "  → %s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        symbol, event.token_mint[:8], rugger_line,
        team_line,
        len(sm_buyers), event.detection_lag_seconds,
        read.verdict, read.confidence * 100,
        factors,
        read.what_would_change,
    )


# ── entry point ───────────────────────────────────────────────────────────────

async def monitor() -> None:
    """Run the graduation monitor forever."""
    gm = GraduationMonitor()
    logger.info("graduation monitor running — waiting for pump.fun graduations")
    await gm.run()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(monitor())
