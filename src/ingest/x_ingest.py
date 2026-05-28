"""X (Twitter) API v2 interface for narrative tracking."""

from typing import Any

import httpx

from src.common.config import settings
from src.common.models import NarrativeMention


class XClient:
    """Async client for X API v2 search endpoints."""

    BASE_URL = "https://api.twitter.com/2"

    def __init__(self, bearer_token: str | None = None) -> None:
        self._bearer_token = bearer_token or settings.x_api_key
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {},
        )

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._http.get(f"{self.BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_recent(
        self, query: str, max_results: int = 100
    ) -> list[dict[str, Any]]:
        """Search recent tweets matching query (last 7 days).

        Requires Elevated or Academic access for max_results > 10.
        """
        max_results = max(10, min(100, max_results))
        data = await self._get(
            "/tweets/search/recent",
            query=query,
            max_results=max_results,
            tweet_fields="author_id,created_at,public_metrics",
            expansions="author_id",
            user_fields="public_metrics,verified",
        )
        return data.get("data") or []

    async def get_user_by_handle(self, handle: str) -> dict[str, Any]:
        """Fetch X user metadata (follower count, verified) by handle."""
        data = await self._get(
            f"/users/by/username/{handle}",
            user_fields="public_metrics,verified,description",
        )
        return data.get("data") or {}

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "XClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


def parse_mention(raw_tweet: dict[str, Any], narrative_id: int) -> NarrativeMention:
    """Map a raw X API tweet to a NarrativeMention dataclass."""
    created_at = raw_tweet.get("created_at", "")
    if created_at:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            posted_at = int(dt.timestamp())
        except ValueError:
            posted_at = 0
    else:
        posted_at = 0

    metrics = (raw_tweet.get("public_metrics") or {})
    text = raw_tweet.get("text", "")
    excerpt = text[:200] if text else None

    return NarrativeMention(
        narrative_id=narrative_id,
        x_handle=raw_tweet.get("author_id", ""),
        posted_at=posted_at,
        follower_count=metrics.get("followers_count"),
        text_excerpt=excerpt,
    )


def is_x_configured() -> bool:
    """Return True if X_API_KEY is set and non-empty."""
    return bool(settings.x_api_key)
