"""Shared logging configuration — and the reason it exists.

httpx logs every request at INFO as a full URL, and several providers take the
API key as a QUERY PARAMETER. The result was 40.3 million log lines in
wallet_watcher.err and 21,914 in graduation_monitor.err containing a live
credential in plaintext, in a 7.9 GB file that anyone with read access — or any
backup, or any support paste — would carry the key out in.

Silencing httpx/httpcore at INFO removes the leak at its source rather than
scrubbing it afterwards, and it is also the single largest source of log volume:
those 40M lines were one failing call repeated, carrying no diagnostic value that
the WARNING-level error does not already carry.
"""

from __future__ import annotations

import logging

# Libraries that log full request URLs (credentials included) at INFO.
_URL_LOGGERS = ("httpx", "httpcore", "urllib3", "aiohttp.client", "websockets.client")


def configure_logging(level: int = logging.INFO, *, fmt: str | None = None) -> None:
    logging.basicConfig(
        level=level,
        format=fmt or "%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    quiet_url_loggers()


def quiet_url_loggers() -> None:
    """Safe to call repeatedly, and safe to call after basicConfig elsewhere."""
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
