import sqlite3
from pathlib import Path

from src.common.config import settings

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "db" / "schema.sql"


def get_connection() -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection to the configured DB path."""
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def migrate() -> None:
    """Run schema.sql against the database if tables don't already exist.

    Idempotent — all CREATE statements use IF NOT EXISTS.
    """
    sql = _SCHEMA_PATH.read_text()
    conn = get_connection()
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
