"""Persistent SQLite storage engine for satellite orbital records."""

from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Optional

from app.config import DATABASE_URL
from app.orbit.exceptions import DatabaseError, RecordValidationError
from app.orbit.models import (
    HistoricalOrbitalRecord,
    IngestionCheckpoint,
    OrbitalRecord,
    StoredOrbitalRecord,
)

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


def compute_record_fingerprint(
    record: HistoricalOrbitalRecord, source: Optional[str] = None
) -> str:
    """Compute a deterministic unique fingerprint for an orbital record.

    If gp_id is present (from Space-Track), returns 'gpid:<gp_id>'.
    Otherwise, returns SHA-256 hash of canonical orbital parameters and source metadata.
    """
    if record.gp_id is not None:
        return f"gpid:{record.gp_id}"

    effective_source = source if source is not None else record.source

    try:
        epoch_ts = parse_epoch_timestamp(record.epoch)
        norm_epoch = f"{epoch_ts:.6f}"
    except Exception:
        norm_epoch = record.epoch.strip()

    norm_creation_date = ""
    if record.creation_date:
        try:
            cd_ts = parse_epoch_timestamp(record.creation_date)
            norm_creation_date = f"{cd_ts:.6f}"
        except Exception:
            norm_creation_date = str(record.creation_date).strip()

    canonical_parts = [
        str(record.norad_cat_id),
        norm_epoch,
        f"{record.mean_motion:.11f}",
        f"{record.eccentricity:.9f}",
        f"{record.inclination:.6f}",
        f"{record.ra_of_asc_node:.6f}",
        f"{record.arg_of_pericenter:.6f}",
        f"{record.mean_anomaly:.6f}",
        f"{(record.bstar or 0.0):.10e}",
        f"{(record.mean_motion_dot or 0.0):.10e}",
        f"{(record.mean_motion_ddot or 0.0):.10e}",
        str(effective_source or "").lower().strip(),
        norm_creation_date,
        str(record.element_set_no if record.element_set_no is not None else ""),
        str(record.rev_at_epoch if record.rev_at_epoch is not None else ""),
    ]
    canonical_str = "|".join(canonical_parts)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


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

CREATE_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orbital_record_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_cat_id INTEGER NOT NULL,
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
    source TEXT NOT NULL DEFAULT 'space-track',
    retrieved_at TEXT NOT NULL,
    creation_date TEXT,
    creation_date_timestamp REAL,
    gp_id INTEGER,
    fingerprint TEXT NOT NULL,
    semimajor_axis REAL,
    period REAL,
    apoapsis REAL,
    periapsis REAL,
    decay_date TEXT,
    tle_line1 TEXT,
    tle_line2 TEXT
);
"""

CREATE_INDEX_HISTORY_NORAD_EPOCH_SQL = """
CREATE INDEX IF NOT EXISTS idx_history_norad_epoch
ON orbital_record_history(norad_cat_id, epoch_timestamp ASC, creation_date_timestamp ASC);
"""

CREATE_INDEX_HISTORY_EPOCH_TS_SQL = """
CREATE INDEX IF NOT EXISTS idx_history_epoch_ts
ON orbital_record_history(epoch_timestamp);
"""

CREATE_INDEX_HISTORY_FINGERPRINT_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_fingerprint
ON orbital_record_history(fingerprint);
"""

CREATE_INDEX_HISTORY_GP_ID_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_gp_id
ON orbital_record_history(gp_id)
WHERE gp_id IS NOT NULL;
"""

CREATE_CHECKPOINTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    norad_cat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    earliest_epoch TEXT,
    earliest_epoch_timestamp REAL,
    latest_epoch TEXT,
    latest_epoch_timestamp REAL,
    record_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (norad_cat_id, source)
);
"""

CREATE_INDEX_CHECKPOINTS_STATUS_SQL = """
CREATE INDEX IF NOT EXISTS idx_checkpoints_status
ON ingestion_checkpoints(status);
"""

INSERT_HISTORY_SQL = """
INSERT INTO orbital_record_history (
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
    retrieved_at,
    creation_date,
    creation_date_timestamp,
    gp_id,
    fingerprint,
    semimajor_axis,
    period,
    apoapsis,
    periapsis,
    decay_date,
    tle_line1,
    tle_line2
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
) ON CONFLICT DO NOTHING;
"""

UPSERT_CHECKPOINT_SQL = """
INSERT INTO ingestion_checkpoints (
    norad_cat_id,
    source,
    status,
    earliest_epoch,
    earliest_epoch_timestamp,
    latest_epoch,
    latest_epoch_timestamp,
    record_count,
    last_error,
    last_attempt_at,
    completed_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(norad_cat_id, source) DO UPDATE SET
    status = excluded.status,
    earliest_epoch = CASE
        WHEN excluded.earliest_epoch_timestamp IS NULL THEN ingestion_checkpoints.earliest_epoch
        WHEN ingestion_checkpoints.earliest_epoch_timestamp IS NULL THEN excluded.earliest_epoch
        WHEN excluded.earliest_epoch_timestamp < ingestion_checkpoints.earliest_epoch_timestamp THEN excluded.earliest_epoch
        ELSE ingestion_checkpoints.earliest_epoch
    END,
    earliest_epoch_timestamp = CASE
        WHEN excluded.earliest_epoch_timestamp IS NULL THEN ingestion_checkpoints.earliest_epoch_timestamp
        WHEN ingestion_checkpoints.earliest_epoch_timestamp IS NULL THEN excluded.earliest_epoch_timestamp
        WHEN excluded.earliest_epoch_timestamp < ingestion_checkpoints.earliest_epoch_timestamp THEN excluded.earliest_epoch_timestamp
        ELSE ingestion_checkpoints.earliest_epoch_timestamp
    END,
    latest_epoch = CASE
        WHEN excluded.latest_epoch_timestamp IS NULL THEN ingestion_checkpoints.latest_epoch
        WHEN ingestion_checkpoints.latest_epoch_timestamp IS NULL THEN excluded.latest_epoch
        WHEN excluded.latest_epoch_timestamp > ingestion_checkpoints.latest_epoch_timestamp THEN excluded.latest_epoch
        ELSE ingestion_checkpoints.latest_epoch
    END,
    latest_epoch_timestamp = CASE
        WHEN excluded.latest_epoch_timestamp IS NULL THEN ingestion_checkpoints.latest_epoch_timestamp
        WHEN ingestion_checkpoints.latest_epoch_timestamp IS NULL THEN excluded.latest_epoch_timestamp
        WHEN excluded.latest_epoch_timestamp > ingestion_checkpoints.latest_epoch_timestamp THEN excluded.latest_epoch_timestamp
        ELSE ingestion_checkpoints.latest_epoch_timestamp
    END,
    record_count = excluded.record_count,
    last_error = excluded.last_error,
    last_attempt_at = excluded.last_attempt_at,
    completed_at = COALESCE(excluded.completed_at, ingestion_checkpoints.completed_at);
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

                # Historical records table and indexes
                self._conn.execute(CREATE_HISTORY_TABLE_SQL)
                self._conn.execute(CREATE_INDEX_HISTORY_NORAD_EPOCH_SQL)
                self._conn.execute(CREATE_INDEX_HISTORY_EPOCH_TS_SQL)
                self._conn.execute(CREATE_INDEX_HISTORY_FINGERPRINT_SQL)
                self._conn.execute(CREATE_INDEX_HISTORY_GP_ID_SQL)

                # Ingestion checkpoints table and index
                self._conn.execute(CREATE_CHECKPOINTS_TABLE_SQL)
                self._conn.execute(CREATE_INDEX_CHECKPOINTS_STATUS_SQL)
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

    def _history_record_to_params(
        self,
        record: HistoricalOrbitalRecord,
        source: Optional[str] = None,
        retrieved_at: Optional[str] = None,
    ) -> tuple:
        """Map HistoricalOrbitalRecord fields to parameter tuple for SQL insert."""
        rec_source = source or record.source or "space-track"
        rec_retrieved_at = (
            retrieved_at or record.retrieved_at or datetime.now(timezone.utc).isoformat()
        )

        epoch_ts = (
            record.epoch_timestamp
            if record.epoch_timestamp is not None
            else parse_epoch_timestamp(record.epoch)
        )

        creation_date_ts = record.creation_date_timestamp
        if creation_date_ts is None and record.creation_date:
            creation_date_ts = parse_epoch_timestamp(record.creation_date)

        if source is not None or record.fingerprint is None:
            fingerprint = compute_record_fingerprint(record, source=rec_source)
        else:
            fingerprint = record.fingerprint

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
            record.ephemeris_type if record.ephemeris_type is not None else 0,
            record.classification_type if record.classification_type is not None else "U",
            record.element_set_no,
            record.rev_at_epoch,
            record.bstar if record.bstar is not None else 0.0,
            record.mean_motion_dot if record.mean_motion_dot is not None else 0.0,
            record.mean_motion_ddot if record.mean_motion_ddot is not None else 0.0,
            rec_source,
            rec_retrieved_at,
            record.creation_date,
            creation_date_ts,
            record.gp_id,
            fingerprint,
            record.semimajor_axis,
            record.period,
            record.apoapsis,
            record.periapsis,
            record.decay_date,
            record.tle_line1,
            record.tle_line2,
        )

    def insert_history_record(
        self,
        record: HistoricalOrbitalRecord,
        source: Optional[str] = None,
        retrieved_at: Optional[str] = None,
    ) -> bool:
        """Insert a single historical orbital record into orbital_record_history.

        Returns:
            True if inserted.
            False if skipped because record already exists (duplicate gp_id or fingerprint).
        """
        conn = self._check_connection()
        params = self._history_record_to_params(record, source, retrieved_at)

        try:
            with self._lock, conn:
                cur = conn.cursor()
                cur.execute(INSERT_HISTORY_SQL, params)
                if cur.rowcount > 0:
                    record.id = cur.lastrowid
                    record.fingerprint = params[23]
                    record.epoch_timestamp = params[4]
                    record.creation_date_timestamp = params[21]
                    return True
                return False
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to insert historical orbital record for NORAD ID {record.norad_cat_id}: {exc}"
            ) from exc

    def batch_insert_history(
        self,
        records: list[HistoricalOrbitalRecord],
        source: Optional[str] = None,
        retrieved_at: Optional[str] = None,
    ) -> tuple[int, int]:
        """Batch insert historical orbital records in a single atomic transaction.

        Returns:
            (inserted_count, skipped_count)
        """
        if not records:
            return (0, 0)

        conn = self._check_connection()
        inserted_count = 0
        skipped_count = 0

        # Pre-validate and generate parameter tuples before executing transaction
        prepared_params = [
            (rec, self._history_record_to_params(rec, source, retrieved_at))
            for rec in records
        ]

        try:
            with self._lock, conn:
                cur = conn.cursor()
                for rec, params in prepared_params:
                    cur.execute(INSERT_HISTORY_SQL, params)
                    if cur.rowcount > 0:
                        inserted_count += 1
                        rec.id = cur.lastrowid
                        rec.fingerprint = params[23]
                        rec.epoch_timestamp = params[4]
                        rec.creation_date_timestamp = params[21]
                    else:
                        skipped_count += 1
            return (inserted_count, skipped_count)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to batch insert historical orbital records: {exc}"
            ) from exc

    def get_history_record_by_id(self, record_id: int) -> Optional[HistoricalOrbitalRecord]:
        """Retrieve a historical orbital record by its internal surrogate ID."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM orbital_record_history WHERE id = ?",
                    (record_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return HistoricalOrbitalRecord.model_validate(dict(row))
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to retrieve historical orbital record ID {record_id}: {exc}"
            ) from exc

    def get_history_record_by_gp_id(self, gp_id: int) -> Optional[HistoricalOrbitalRecord]:
        """Retrieve a historical orbital record by Space-Track GP_ID."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM orbital_record_history WHERE gp_id = ?",
                    (gp_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return HistoricalOrbitalRecord.model_validate(dict(row))
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to retrieve historical orbital record for GP_ID {gp_id}: {exc}"
            ) from exc

    def query_history_range(
        self,
        norad_cat_id: int,
        start_epoch: Optional[str] = None,
        end_epoch: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        order_desc: bool = False,
    ) -> list[HistoricalOrbitalRecord]:
        """Query historical orbital records for a satellite ordered chronologically.

        Args:
            norad_cat_id: Satellite NORAD catalog number.
            start_epoch: Optional start of range (inclusive ISO 8601 UTC timestamp).
            end_epoch: Optional end of range (inclusive ISO 8601 UTC timestamp).
            limit: Maximum number of records to return (default: 100).
            offset: Pagination offset (default: 0).
            order_desc: If True, order DESC (newest epoch first); otherwise ASC (default: False).

        Returns:
            List of HistoricalOrbitalRecord ordered by epoch_timestamp,
            creation_date_timestamp, id.
        """
        conn = self._check_connection()
        query = ["SELECT * FROM orbital_record_history WHERE norad_cat_id = ?"]
        params: list[Any] = [norad_cat_id]

        if start_epoch:
            start_ts = parse_epoch_timestamp(start_epoch)
            query.append("AND epoch_timestamp >= ?")
            params.append(start_ts)

        if end_epoch:
            end_ts = parse_epoch_timestamp(end_epoch)
            query.append("AND epoch_timestamp <= ?")
            params.append(end_ts)

        order_dir = "DESC" if order_desc else "ASC"
        query.append(
            f"ORDER BY epoch_timestamp {order_dir}, creation_date_timestamp {order_dir}, id {order_dir} LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        sql = " ".join(query)
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [HistoricalOrbitalRecord.model_validate(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to query historical orbital records for NORAD ID {norad_cat_id}: {exc}"
            ) from exc

    def find_prior_history_candidates(
        self,
        norad_cat_id: int,
        target_timestamp: float,
        limit: int = 20,
    ) -> list[HistoricalOrbitalRecord]:
        """Query closest prior historical records for a satellite ordered by epoch DESC, creation_date DESC, gp_id DESC, id DESC."""
        conn = self._check_connection()
        sql = (
            "SELECT * FROM orbital_record_history "
            "WHERE norad_cat_id = ? AND epoch_timestamp <= ? "
            "ORDER BY epoch_timestamp DESC, creation_date_timestamp DESC, gp_id DESC, id DESC "
            "LIMIT ?"
        )
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(sql, (norad_cat_id, target_timestamp, limit))
                rows = cur.fetchall()
                return [HistoricalOrbitalRecord.model_validate(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to query prior historical candidates for NORAD ID {norad_cat_id}: {exc}"
            ) from exc

    def find_subsequent_history_candidates(
        self,
        norad_cat_id: int,
        target_timestamp: float,
        limit: int = 5,
    ) -> list[HistoricalOrbitalRecord]:
        """Query closest subsequent historical records for a satellite ordered by epoch ASC, creation_date DESC, gp_id DESC, id DESC."""
        conn = self._check_connection()
        sql = (
            "SELECT * FROM orbital_record_history "
            "WHERE norad_cat_id = ? AND epoch_timestamp > ? "
            "ORDER BY epoch_timestamp ASC, creation_date_timestamp DESC, gp_id DESC, id DESC "
            "LIMIT ?"
        )
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(sql, (norad_cat_id, target_timestamp, limit))
                rows = cur.fetchall()
                return [HistoricalOrbitalRecord.model_validate(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to query subsequent historical candidates for NORAD ID {norad_cat_id}: {exc}"
            ) from exc

    def count_history_records(
        self,
        norad_cat_id: Optional[int] = None,
        source: Optional[str] = None,
    ) -> int:
        """Count historical orbital records, optionally filtered by NORAD catalog ID and/or source."""
        conn = self._check_connection()
        query = ["SELECT COUNT(*) FROM orbital_record_history WHERE 1=1"]
        params: list[Any] = []
        if norad_cat_id is not None:
            query.append("AND norad_cat_id = ?")
            params.append(norad_cat_id)
        if source is not None:
            query.append("AND source = ?")
            params.append(source)
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(" ".join(query), params)
                return cur.fetchone()[0]
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to count historical orbital records: {exc}"
            ) from exc

    def clear_history(self) -> None:
        """Clear all historical orbital records (primarily for testing)."""
        conn = self._check_connection()
        try:
            with self._lock, conn:
                conn.execute("DELETE FROM orbital_record_history")
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to clear historical orbital records: {exc}") from exc

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        """Insert or update an ingestion progress checkpoint."""
        conn = self._check_connection()

        earliest_ts = checkpoint.earliest_epoch_timestamp
        if earliest_ts is None and checkpoint.earliest_epoch:
            earliest_ts = parse_epoch_timestamp(checkpoint.earliest_epoch)

        latest_ts = checkpoint.latest_epoch_timestamp
        if latest_ts is None and checkpoint.latest_epoch:
            latest_ts = parse_epoch_timestamp(checkpoint.latest_epoch)

        params = (
            checkpoint.norad_cat_id,
            checkpoint.source,
            checkpoint.status,
            checkpoint.earliest_epoch,
            earliest_ts,
            checkpoint.latest_epoch,
            latest_ts,
            checkpoint.record_count,
            checkpoint.last_error,
            checkpoint.last_attempt_at,
            checkpoint.completed_at,
        )

        try:
            with self._lock, conn:
                cur = conn.cursor()
                cur.execute(UPSERT_CHECKPOINT_SQL, params)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to save ingestion checkpoint for NORAD {checkpoint.norad_cat_id} ({checkpoint.source}): {exc}"
            ) from exc

    def update_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        """Alias for save_checkpoint."""
        self.save_checkpoint(checkpoint)

    def get_checkpoint(
        self, norad_cat_id: int, source: str
    ) -> Optional[IngestionCheckpoint]:
        """Retrieve ingestion checkpoint by NORAD ID and source."""
        conn = self._check_connection()
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM ingestion_checkpoints WHERE norad_cat_id = ? AND source = ?",
                    (norad_cat_id, source),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return IngestionCheckpoint.model_validate(dict(row))
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to retrieve checkpoint for NORAD {norad_cat_id} ({source}): {exc}"
            ) from exc

    def list_checkpoints(
        self, source: Optional[str] = None, status: Optional[str] = None
    ) -> list[IngestionCheckpoint]:
        """List ingestion checkpoints, optionally filtered by source or status."""
        conn = self._check_connection()
        query = ["SELECT * FROM ingestion_checkpoints WHERE 1=1"]
        params: list[Any] = []
        if source is not None:
            query.append("AND source = ?")
            params.append(source)
        if status is not None:
            query.append("AND status = ?")
            params.append(status)
        query.append("ORDER BY norad_cat_id ASC, source ASC")

        sql = " ".join(query)
        try:
            with self._lock:
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [IngestionCheckpoint.model_validate(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to list ingestion checkpoints: {exc}") from exc

    def clear_checkpoints(self) -> None:
        """Clear all ingestion checkpoints (primarily for testing)."""
        conn = self._check_connection()
        try:
            with self._lock, conn:
                conn.execute("DELETE FROM ingestion_checkpoints")
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to clear ingestion checkpoints: {exc}") from exc

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
