"""Background service — clusters X mentions into active narratives."""

import asyncio
import json
import logging

from src.common.db import get_connection
from src.common.models import NarrativeState

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 300          # seconds between full narrative sweeps
VELOCITY_HOT_THRESHOLD = 50  # mentions/hour to become "hot"
VELOCITY_FADING_THRESHOLD = 5


async def track_narratives() -> None:
    """Main entry point — runs indefinitely until cancelled."""
    from src.ingest.x_ingest import is_x_configured

    if not is_x_configured():
        logger.warning("X_API_KEY not set — narrative tracking disabled")
        return

    logger.info("narrative tracker starting")
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("narrative tracker tick failed")
        await asyncio.sleep(SCAN_INTERVAL)


async def _tick() -> None:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, label, keywords, started_at, peak_velocity, current_velocity, status "
            "FROM narratives WHERE status != 'dead'"
        ).fetchall()
        narratives = [
            NarrativeState(
                id=row["id"],
                label=row["label"],
                keywords=json.loads(row["keywords"]) if isinstance(row["keywords"], str) else row["keywords"],
                started_at=row["started_at"],
                peak_velocity=float(row["peak_velocity"]),
                current_velocity=float(row["current_velocity"]),
                status=row["status"],
            )
            for row in rows
        ]
    finally:
        conn.close()

    tasks = [refresh_narrative(n) for n in narratives]
    await asyncio.gather(*tasks, return_exceptions=True)


async def refresh_narrative(narrative: NarrativeState) -> None:
    """Fetch new mentions for a single narrative and update velocity/status in DB."""
    from src.ingest.x_ingest import XClient, parse_mention

    keywords_query = " OR ".join(narrative.keywords[:5])
    query = f"({keywords_query}) lang:en -is:retweet"

    try:
        async with XClient() as x:
            tweets = await x.search_recent(query, max_results=100)
    except Exception:
        logger.exception("failed to fetch mentions for '%s'", narrative.label)
        return

    conn = get_connection()
    try:
        for tweet in tweets:
            mention = parse_mention(tweet, narrative.id)
            conn.execute(
                """INSERT OR IGNORE INTO narrative_mentions
                   (narrative_id, x_handle, posted_at, follower_count, text_excerpt)
                   VALUES (?, ?, ?, ?, ?)""",
                (mention.narrative_id, mention.x_handle, mention.posted_at,
                 mention.follower_count, mention.text_excerpt),
            )

        velocity = compute_velocity(narrative.id, conn=conn)
        new_peak = max(narrative.peak_velocity, velocity)
        status = classify_status(velocity, new_peak)

        conn.execute(
            """UPDATE narratives
               SET current_velocity = ?, peak_velocity = ?, status = ?
               WHERE id = ?""",
            (velocity, new_peak, status, narrative.id),
        )
        conn.commit()
    finally:
        conn.close()


def compute_velocity(
    narrative_id: int,
    window_seconds: int = 3600,
    conn: "object | None" = None,
) -> float:
    """Calculate mention velocity for a narrative over the given rolling window."""
    import time

    _own_conn = conn is None
    if _own_conn:
        conn = get_connection()
    try:
        cutoff = int(time.time()) - window_seconds
        row = conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM narrative_mentions WHERE narrative_id = ? AND posted_at >= ?",
            (narrative_id, cutoff),
        ).fetchone()
        count = row[0] if row else 0
        return count * 3600.0 / window_seconds
    finally:
        if _own_conn:
            conn.close()  # type: ignore[union-attr]


def classify_status(current_velocity: float, peak_velocity: float) -> str:
    """Map velocity to a narrative status string."""
    if current_velocity >= VELOCITY_HOT_THRESHOLD:
        return "hot"
    if current_velocity >= VELOCITY_FADING_THRESHOLD:
        return "emerging"
    if peak_velocity > 0 and current_velocity < VELOCITY_FADING_THRESHOLD:
        return "fading"
    return "dead"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(track_narratives())
