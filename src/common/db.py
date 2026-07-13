import sqlite3
from pathlib import Path

from src.common.config import settings

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "db" / "schema.sql"


def get_connection() -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection to the configured DB path."""
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    # Wait up to 30s for a competing writer (e.g. the live service) instead of
    # immediately raising "database is locked" — WAL allows concurrent readers
    # but serialises writers.
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def migrate() -> None:
    """Run schema.sql against the database if tables don't already exist.

    Idempotent — all CREATE statements use IF NOT EXISTS.
    Also applies ALTER TABLE additions for columns added after initial release.
    """
    sql = _SCHEMA_PATH.read_text()
    conn = get_connection()
    try:
        conn.executescript(sql)
        conn.commit()
        _add_column_if_missing(conn, "graduation_events", "smart_money_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "graduation_events", "dominant_factors_json", "TEXT NOT NULL DEFAULT '[]'")
        # Pipeline v2: real created_at + structural-account capture (see structural_accounts.py)
        _add_column_if_missing(conn, "tokens", "created_at_source", "TEXT")
        _add_column_if_missing(conn, "tokens", "created_on", "TEXT")
        # on-chain platform verdict ('pump.fun'/'mayhem'/foreign createdOn/'pump.fun*');
        # separate column because launchpad carries a legacy CHECK constraint
        _add_column_if_missing(conn, "tokens", "platform", "TEXT")
        # manufactured graduations (one entity buys the curve) — flagged, kept in the
        # DB and live pipeline, EXCLUDED from model/label populations
        _add_column_if_missing(conn, "graduation_events", "is_manufactured", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "graduation_events", "manufactured_flags", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(conn, "tokens", "creator_wallet", "TEXT")
        _add_column_if_missing(conn, "tokens", "total_supply", "REAL")
        _add_column_if_missing(conn, "graduation_events", "migration_venue", "TEXT")
        _add_column_if_missing(conn, "graduation_events", "amm_pool_address", "TEXT")
        _add_column_if_missing(conn, "graduation_events", "pool_accounts_json", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(conn, "graduation_events", "pipeline_version", "INTEGER NOT NULL DEFAULT 1")
        # Memory system columns
        _add_column_if_missing(conn, "funder_reputation", "launches_24h", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "funder_reputation", "launches_7d", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "funder_reputation", "velocity_updated", "INTEGER")
        _add_column_if_missing(conn, "funder_reputation", "avg_dump_start_h", "REAL")
        _add_column_if_missing(conn, "funder_reputation", "dump_start_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "team_fingerprints", "avg_first_buy_offset_s", "REAL NOT NULL DEFAULT 0.0")
        _add_column_if_missing(conn, "team_fingerprints", "avg_sniper_rate", "REAL NOT NULL DEFAULT 0.0")
        _add_column_if_missing(conn, "team_fingerprints", "sample_count", "INTEGER NOT NULL DEFAULT 0")
        # Post-grad swap tracking aggregate columns
        _add_column_if_missing(conn, "post_grad_behavior", "team_buy_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "post_grad_behavior", "team_sell_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "post_grad_behavior", "team_net_sol", "REAL")
        _add_column_if_missing(conn, "post_grad_behavior", "coordinated_sell_count", "INTEGER NOT NULL DEFAULT 0")
        # Whole-tape aggregates (retail vs team flow)
        _add_column_if_missing(conn, "post_grad_behavior", "total_buy_count", "INTEGER")
        _add_column_if_missing(conn, "post_grad_behavior", "total_sell_count", "INTEGER")
        _add_column_if_missing(conn, "post_grad_behavior", "unique_buyers", "INTEGER")
        _add_column_if_missing(conn, "post_grad_behavior", "retail_net_sol", "REAL")
        # Phase B: slot-level microstructure features on bc_flow_features
        _add_column_if_missing(conn, "bc_flow_features", "launch_slot_snipe_count", "INTEGER")
        _add_column_if_missing(conn, "bc_flow_features", "buys_first_slot", "INTEGER")
        _add_column_if_missing(conn, "bc_flow_features", "buys_first_3_slots", "INTEGER")
        _add_column_if_missing(conn, "bc_flow_features", "distinct_slots_first_20_buys", "INTEGER")
        _add_column_if_missing(conn, "bc_flow_features", "max_same_slot_group", "INTEGER")
        _add_column_if_missing(conn, "bc_flow_features", "bundled_adjacent_count", "INTEGER")
        # Phase D: exit-choreography rollup on team_fingerprints
        _add_column_if_missing(conn, "team_fingerprints", "avg_exit_spread_s", "REAL")
        _add_column_if_missing(conn, "team_fingerprints", "leader_wallet", "TEXT")
        _add_column_if_missing(conn, "team_fingerprints", "leader_consistency", "REAL")
        _add_column_if_missing(conn, "team_fingerprints", "choreography_sample_count", "INTEGER NOT NULL DEFAULT 0")
        # v4 trajectory heads (the targets that actually matter)
        _add_column_if_missing(conn, "model_predictions", "p_survive60", "REAL")
        _add_column_if_missing(conn, "model_predictions", "p_team_exit10", "REAL")
        _add_column_if_missing(conn, "model_predictions", "p_moon10x", "REAL")
        _add_column_if_missing(conn, "model_predictions", "p_fastrug", "REAL")
        # Holder/whale tracking
        _add_column_if_missing(conn, "post_grad_swaps", "is_smart_money", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "post_grad_swaps", "tx_signature", "TEXT")
        _add_column_if_missing(conn, "post_grad_swaps", "price_usd", "REAL")
        # Coordination tables gained a `phase` column + composite PK — recreate the
        # old (phase-less) shape if present. Held only throwaway post-grad rows.
        _recreate_if_missing_column(conn, "coin_coordination", "phase")
        _recreate_if_missing_column(conn, "coordinated_entities", "phase")
        # team_fingerprints.funding_source must be UNIQUE for ON CONFLICT upserts;
        # replace the old non-unique index (table was reset at pipeline v2).
        conn.execute("DROP INDEX IF EXISTS idx_fingerprints_funding_source")
        conn.executescript(sql)   # re-run CREATE IF NOT EXISTS to rebuild dropped tables
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _recreate_if_missing_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """Drop a table if it exists but lacks `column` (PK change needs a rebuild).

    Only safe for tables whose contents are cheaply regenerated. The subsequent
    executescript re-creates the table with the current schema.
    """
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    if info and column not in {row[1] for row in info}:
        conn.execute(f"DROP TABLE {table}")
