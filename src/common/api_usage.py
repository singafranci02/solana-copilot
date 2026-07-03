"""Best-effort per-day API request accounting (api_usage table).

Ground truth for the Solana Tracker monthly budget (200k requests/month).
Counts are buffered in memory and flushed in small batches so the hot request
path never blocks on SQLite. A failed flush is dropped silently — accounting
must never break a data fetch.
"""

import logging
import re
import time
from collections import Counter

logger = logging.getLogger(__name__)

_FLUSH_EVERY = 25          # pending increments before a flush attempt
_FLUSH_MAX_AGE_S = 60.0    # flush at least this often while traffic flows

_pending: Counter = Counter()
_last_flush = time.monotonic()

# Base58 addresses (32-44 chars) and tx signatures → collapse to a template
_ADDRESS_SEG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{30,}$")


def normalize_endpoint(path: str) -> str:
    """Collapse address-bearing path segments so endpoints aggregate cleanly."""
    return "/".join(
        "{mint}" if _ADDRESS_SEG.match(seg) else seg
        for seg in path.split("/")
    )


def record(provider: str, endpoint: str) -> None:
    """Count one request. Never raises."""
    global _last_flush
    try:
        _pending[(time.strftime("%Y-%m-%d", time.gmtime()), provider, normalize_endpoint(endpoint))] += 1
        now = time.monotonic()
        if sum(_pending.values()) >= _FLUSH_EVERY or now - _last_flush >= _FLUSH_MAX_AGE_S:
            flush()
    except Exception:
        pass


def flush() -> None:
    """Write pending counts to SQLite. Best-effort; drops the batch on failure."""
    global _last_flush
    if not _pending:
        return
    batch = dict(_pending)
    _pending.clear()
    _last_flush = time.monotonic()
    try:
        from src.common.db import get_connection
        conn = get_connection()
        try:
            conn.executemany(
                """INSERT INTO api_usage (day, provider, endpoint, count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(day, provider, endpoint)
                   DO UPDATE SET count = count + excluded.count""",
                [(day, prov, ep, n) for (day, prov, ep), n in batch.items()],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("api_usage flush dropped: %s", exc)
