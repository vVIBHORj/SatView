"""Persistent SQLite storage engine for satellite orbital records."""

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Optional

from app.config import DATABASE_URL
from app.orbit.exceptions import DatabaseError, RecordValidationError
from app.orbit.models import OrbitalRecord, StoredOrbitalRecord

logger = logging.getLogger(__name__)


def resolve_sqlite_path(db_url: str = DATABASE_URL) -> str:
    """Resolve a database URL or path to a clean SQLite connection string."""
    if not db_url or db_url == ":memory:":
        return ":memory:"

    clean = db_url
    if clean.startswith("sqlite:///"):
        clean = clean[len("sqlite:///"):]
    elif clean.startswith("sqlite://"):
        clean = clean[len("sqlite://"):]

    if clean == ":memory:":
        return ":memory:"

    resolved = Path(clean).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def parse_epoch_timestamp(epoch_str: str) -> float:
    """Parse an ISO 8601 epoch string into a microsecond-accurate UTC timestamp (seconds)."""
    cleaned = epoch_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(cleaned)
    except Exception as exc:
        raise RecordValidationError(f"Invalid epoch timestamp '{epoch_str}': {exc}") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.timestamp()


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orbital_records (
    norad_cat_id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL,
    object_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    epoch_timestamp REAL NOT NULL,
    mean_motion REAL NOT NULL,
    eccentricity REAL NOT NULL,
    inclination REAL NOT NULL,
    ra_of_asc_node REAL NOT NULL,
    arg_of_pericenter REAL NOT NULL,
    mean_anomaly REAL NOT NULL,
    ephemeris_type INTEGER DEFAULT 0,
    classification_type TEXT DEFAULT 'U',
    element_set_no INTEGER,
    rev_at_epoch INTEGER,
    bstar REAL DEFAULT 0.0,
    mean_motion_dot REAL DEFAULT 0.0,
    mean_motion_ddot REAL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'celestrak',
    retrieved_at TEXT NOT NULL
);
"""

CREATE_INDEX_EPOCH_SQL = """
CREATE INDEX IF NOT EXISTS idx_orbital_records_epoch_ts
ON orbital_records(epoch_timestamp);
"""

CREATE_INDEX_NAME_SQL = """
CREATE INDEX IF NOT EXISTS idx_orbital_records_object_name
ON orbital_records(object_name);
"""

UPSERT_SQL = """
INSERT INTO orbital_records (
    norad_cat_id,
    object_name,
    object_id,
    epoch,
    epoch_timestamp,
    mean_motion,
    eccentricity,
    inclination,
    ra_of_asc_node,
    arg_of_pericenter,
    mean_anomaly,
    ephemeris_type,
    classification_type,
    element_set_no,
    rev_at_epoch,
    bstar,
    mean_motion_dot,
    mean_motion_ddot,
    source,
    retrieved_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(norad_cat_id) DO UPDATE SET
    object_name = excluded.object_name,
    object_id = excluded.object_id,
    epoch = excluded.epoch,
    epoch_timestamp = excluded.epoch_timestamp,
    mean_motion = excluded.mean_motion,
    eccentricity = excluded.eccentricity,
    inclination = excluded.inclination,
    ra_of_asc_node = excluded.ra_of_asc_node,
    arg_of_pericenter = excluded.arg_of_pericenter,
    mean_anomaly = excluded.mean_anomaly,
    ephemeris_type = excluded.ephemeris_type,
    classification_type = excluded.classification_type,
    element_set_no = excluded.element_set_no,
    rev_at_epoch = excluded.rev_at_epoch,
    bstar = excluded.bstar,
    mean_motion_dot = excluded.mean_motion_dot,
    mean_motion_ddot = excluded.mean_motion_ddot,
    source = excluded.source,
    retrieved_at = excluded.retrieved_at
WHERE excluded.epoch_timestamp >= orbital_records.epoch_timestamp;
"""


class OrbitStorage:
    """Thread-safe persistent SQLite storage for validated satellite orbital records."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url if db_url is not None else DATABASE_URL
        self.db_path = resolve_sqlite_path(self.db_url)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._is_closed = False
        self._connect()
        self._init_db()

    def _connect(self) -> None:
        """Establish SQLite connection."""
        try:
            self._conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to connect to SQLite database at '{self.db_path}': {exc}") from exc

    def _init_db(self) -> None:
        """Initialize database schema, indices, and performance pragmas."""
        try:
            with self._lock, self._conn:
                if self.db_path != ":memory:":
                    # Enable Write-Ahead Logging for high concurrency on file databases
                    self._conn.execute("PRAGMA journal_mode = WAL;")
                    self._conn.execute("PRAGMA synchronous = NORMAL;")
                self._conn.execute(CREATE_TABLE_SQL)
                self._conn.execute(CREATE_INDEX_EPOCH_SQL)
                self._conn.execute(CREATE_INDEX_NAME_SQL)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to initialize orbital database schema: {exc}") from exc

    def _check_connection(self) -> sqlite3.Connection:
        """Ensure connection is open and active."""
        if self._is_closed or self._conn is None:
            raise DatabaseError("Database connection is closed")
        return self._conn

    def _record_to_params(
        self, record: OrbitalRecord, source: str, retrieved_at: str, epoch_ts: float
    ) -> tuple:
        """Map OrbitalRecord fields to parameter tuple for SQL queries."""
        return (
            record.norad_cat_id,
            record.object_name,
            record.object_id,
            record.epoch,
            epoch_ts,
            record.mean_motion,
            record.eccentricity,
            record.inclination,
            record.ra_of_asc_node,
            record.arg_of_pericenter,
            record.mean_anomaly,
            record.ephemeris_type,
            record.classification_type,
            record.element_set_no,
            record.rev_at_epoch,
            record.bstar,
            record.mean_motion_dot,
            record.mean_motion_ddot,
            source,
            retrieved_at,
        )

    def upsert(
        self,
        record: OrbitalRecord,
        source: str = "celestrak",
        retrieved_at: Optional[str] = None,
    ) -> bool:
        """Insert or update an orbital record if newer than stored record.

        Returns:
            True if inserted or updated.
            False if skipped because stored record has a newer epoch (prevent stale overwrite).
        """
        conn = self._check_connection()
        if retrieved_at is None:
            retrieved_at = datetime.now(timezone.utc).isoformat()
        epoch_ts = parse_epoch_timestamp(record.epoch)
        params = self._record_to_params(record, source, retrieved_at, epoch_ts)

        try:
            with self._lock, conn:
                cur = conn.cursor()
                cur.execute(UPSERT_SQL, params)
                was_modified = cur.rowcount > 0
                return was_modified
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to upsert orbital record for NORAD ID {record.norad_cat_id}: {exc}"
            ) from exc

    def upsert_many(
        self,
        records: list[OrbitalRecord],
        source: str = "celestrak",
        retrieved_at: Optional[str] = None,
    ) -> int:
        """Batch upsert multiple orbital records in a single transaction.

        Returns:
            Count of records successfully inserted or updated.
        """
        if not records:
            return 0

        conn = self._check_connection()
        if retrieved_at is None:
            retrieved_at = datetime.now(timezone.utc).isoformat()

        updated_count = 0
        try:
            with self._lock, conn:
                cur = conn.cursor()
                for rec in records:
                    epoch_ts = parse_epoch_timestamp(rec.epoch)
                    params = self._record_to_params(rec, source, retrieved_at, epoch_ts)
                    cur.execute(UPSERT_SQL, params)
                    if cur.rowcount > 0:
                        updated_count += 1
            return updated_count
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to batch upsert orbital records: {exc}") from exc

    def get(self, norad_cat_id: int) -> Optional[StoredOrbitalRecord]:
        """Retrieve stored orbital record with provenance metadata by NORAD catalog ID."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM orbital_records WHERE norad_cat_id = ?",
                    (norad_cat_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return StoredOrbitalRecord.model_validate(dict(row))
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to retrieve stored orbital record for NORAD ID {norad_cat_id}: {exc}"
            ) from exc

    def get_record(self, norad_cat_id: int) -> Optional[OrbitalRecord]:
        """Convenience method to retrieve stored record as base OrbitalRecord."""
        stored = self.get(norad_cat_id)
        return stored.to_orbital_record() if stored is not None else None

    def list_records(
        self, limit: int = 100, offset: int = 0
    ) -> list[StoredOrbitalRecord]:
        """List stored orbital records ordered by NORAD catalog ID."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM orbital_records ORDER BY norad_cat_id ASC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                rows = cur.fetchall()
                return [StoredOrbitalRecord.model_validate(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to list stored orbital records: {exc}") from exc

    def count(self) -> int:
        """Count total stored orbital records."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM orbital_records")
                return cur.fetchone()[0]
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to count stored orbital records: {exc}") from exc

    def delete(self, norad_cat_id: int) -> bool:
        """Delete an orbital record by NORAD catalog ID. Returns True if record was deleted."""
        conn = self._check_connection()
        try:
            with self._lock, conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM orbital_records WHERE norad_cat_id = ?",
                    (norad_cat_id,),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to delete orbital record for NORAD ID {norad_cat_id}: {exc}"
            ) from exc

    def clear(self) -> None:
        """Clear all stored orbital records (primarily for testing)."""
        conn = self._check_connection()
        try:
            with self._lock, conn:
                conn.execute("DELETE FROM orbital_records")
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to clear orbital records: {exc}") from exc

    def close(self) -> None:
        """Close SQLite database connection."""
        with self._lock:
            if not self._is_closed and self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._is_closed = True
                self._conn = None

    def __enter__(self) -> "OrbitStorage":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
