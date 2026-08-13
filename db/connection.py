# SQLite connection factory for ChilliFlake.
# Responsibilities: bootstrap the schema on first connect, enforce foreign keys,
# enable WAL mode for concurrent reads, and return a ready-to-use connection.

import sqlite3
import os
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at *db_path* and return a connection.

    * WAL journal mode — allows one writer + many concurrent readers.
    * foreign_keys = ON — enforced at the connection level (SQLite default is OFF).
    * Schema is bootstrapped from schema.sql using CREATE TABLE IF NOT EXISTS,
      so this call is safe to make on every startup.

    Args:
        db_path: Filesystem path to the .db file.  Falls back to the
                 DB_PATH environment variable, then 'db/chilliflake.db'.

    Returns:
        sqlite3.Connection with row_factory = sqlite3.Row so callers can
        address columns by name.

    Raises:
        RuntimeError: if the schema file cannot be found.
    """
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "db/chilliflake.db")

    # Ensure parent directory exists (important for a fresh checkout).
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Pragmas must be set before any DDL.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    # Bootstrap schema idempotently.
    if not _SCHEMA_PATH.exists():
        raise RuntimeError(f"Schema file not found: {_SCHEMA_PATH}")

    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)

    return conn
