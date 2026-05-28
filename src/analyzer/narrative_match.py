"""Match a token to active narratives tracked by the narrative_tracker service."""

import json
import sqlite3

from src.common.models import NarrativeState, Token


def get_active_narratives(conn: sqlite3.Connection) -> list[NarrativeState]:
    """Return narratives with status in ('emerging', 'hot'), ordered by velocity."""
    rows = conn.execute(
        """SELECT id, label, keywords, started_at, peak_velocity, current_velocity, status
           FROM narratives
           WHERE status IN ('emerging', 'hot')
           ORDER BY current_velocity DESC"""
    ).fetchall()
    return [
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


def _levenshtein(a: str, b: str) -> int:
    """Compute edit distance between two strings."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def match_token_to_narratives(
    token: Token,
    narratives: list[NarrativeState],
) -> list[str]:
    """Score token name/symbol against narrative keyword lists.

    Checks exact substring matches first; falls back to fuzzy (Levenshtein ≤ 2)
    for keywords/fields that are at least 4 characters long to avoid false positives.
    Also checks token.narrative_tags directly.
    """
    text_fields = {
        token.name.lower(),
        token.symbol.lower(),
        *[t.lower() for t in token.narrative_tags],
    }

    matched: list[str] = []
    for narrative in narratives:
        found = False
        for keyword in narrative.keywords:
            kw = keyword.lower()
            for field_val in text_fields:
                if kw in field_val or field_val in kw:
                    found = True
                    break
                if len(kw) >= 4 and len(field_val) >= 4 and _levenshtein(kw, field_val) <= 2:
                    found = True
                    break
            if found:
                break
        if found:
            matched.append(narrative.label)

    return matched


def narrative_velocity_at_entry(
    narrative_label: str,
    entry_ts: int,
    conn: sqlite3.Connection,
) -> float:
    """Return the mention velocity (mentions/hour) for a narrative in the 1h before entry_ts."""
    row = conn.execute(
        """SELECT COUNT(*) FROM narrative_mentions nm
           JOIN narratives n ON n.id = nm.narrative_id
           WHERE n.label = ?
             AND nm.posted_at >= ?
             AND nm.posted_at < ?""",
        (narrative_label, entry_ts - 3600, entry_ts),
    ).fetchone()
    return float(row[0]) if row else 0.0
