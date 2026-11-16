"""Deterministic tests for historical orbital storage, fingerprinting, and ingestion checkpoints."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import pytest

from app.orbit.exceptions import DatabaseError, RecordValidationError
from app.orbit.models import HistoricalOrbitalRecord, IngestionCheckpoint, OrbitalRecord
from app.orbit.storage import (
    CREATE_INDEX_EPOCH_SQL,
    CREATE_INDEX_NAME_SQL,
    CREATE_TABLE_SQL,
    OrbitStorage,
    compute_record_fingerprint,
    parse_epoch_timestamp,
)


@pytest.fixture
def memory_storage() -> OrbitStorage:
    """Fixture providing an isolated in-memory OrbitStorage instance."""
    storage = OrbitStorage(db_url=":memory:")
    yield storage
    storage.close()


def make_history_record(
    norad_cat_id: int = 25544,
    object_name: str = "ISS (ZARYA)",
    object_id: str = "1998-067A",
    epoch: str = "2024-01-01T00:00:00Z",
    mean_motion: float = 15.5,
    eccentricity: float = 0.0001,
    inclination: float = 51.64,
    ra_of_asc_node: float = 120.0,
    arg_of_pericenter: float = 60.0,
    mean_anomaly: float = 30.0,
    gp_id: int | None = None,
    source: str = "space-track",
    creation_date: str | None = None,
    element_set_no: int | None = 999,
    rev_at_epoch: int | None = 1000,
    bstar: float = 0.00002,
    mean_motion_dot: float = 0.00001,
    mean_motion_ddot: float = 0.0,
    semimajor_axis: float | None = 6780.0,
    period: float | None = 92.8,
    apoapsis: float | None = 420.0,
    periapsis: float | None = 410.0,
    decay_date: str | None = None,
    tle_line1: str | None = None,
    tle_line2: str | None = None,
) -> HistoricalOrbitalRecord:
    """Helper to generate valid HistoricalOrbitalRecord test instances."""
    return HistoricalOrbitalRecord(
        norad_cat_id=norad_cat_id,
        object_name=object_name,
        object_id=object_id,
        epoch=epoch,
        mean_motion=mean_motion,
        eccentricity=eccentricity,
        inclination=inclination,
        ra_of_asc_node=ra_of_asc_node,
        arg_of_pericenter=arg_of_pericenter,
        mean_anomaly=mean_anomaly,
        gp_id=gp_id,
        source=source,
        creation_date=creation_date,
        element_set_no=element_set_no,
        rev_at_epoch=rev_at_epoch,
        bstar=bstar,
        mean_motion_dot=mean_motion_dot,
        mean_motion_ddot=mean_motion_ddot,
        semimajor_axis=semimajor_axis,
        period=period,
        apoapsis=apoapsis,
        periapsis=periapsis,
        decay_date=decay_date,
        tle_line1=tle_line1,
        tle_line2=tle_line2,
    )


# ---------------------------------------------------------------------------
# 1. Fingerprint Computation Tests
# ---------------------------------------------------------------------------


def test_fingerprint_with_gp_id() -> None:
    """Records with Space-Track GP_ID produce a deterministic 'gpid:<id>' fingerprint."""
    rec = make_history_record(gp_id=12345678)
    fp = compute_record_fingerprint(rec)
    assert fp == "gpid:12345678"


def test_fingerprint_without_gp_id_deterministic() -> None:
    """Records without GP_ID produce a 64-char SHA-256 hex string identical across instances."""
    rec1 = make_history_record(gp_id=None, creation_date="2024-01-01T01:00:00Z")
    rec2 = make_history_record(gp_id=None, creation_date="2024-01-01T01:00:00Z")

    fp1 = compute_record_fingerprint(rec1)
    fp2 = compute_record_fingerprint(rec2)

    assert len(fp1) == 64
    assert fp1 == fp2


def test_fingerprint_distinguishes_different_creation_dates() -> None:
    """Records with the same epoch but distinct creation dates have distinct fingerprints."""
    rec1 = make_history_record(gp_id=None, epoch="2024-01-01T00:00:00Z", creation_date="2024-01-01T01:00:00Z")
    rec2 = make_history_record(gp_id=None, epoch="2024-01-01T00:00:00Z", creation_date="2024-01-01T02:00:00Z")

    assert compute_record_fingerprint(rec1) != compute_record_fingerprint(rec2)


def test_fingerprint_distinguishes_different_orbital_elements() -> None:
    """Records with the same epoch and creation date but distinct elements have distinct fingerprints."""
    rec1 = make_history_record(gp_id=None, mean_motion=15.5)
    rec2 = make_history_record(gp_id=None, mean_motion=15.6)

    assert compute_record_fingerprint(rec1) != compute_record_fingerprint(rec2)


def test_fingerprint_handles_missing_optional_fields() -> None:
    """compute_record_fingerprint works without error when optional fields are None."""
    rec = make_history_record(
        gp_id=None,
        creation_date=None,
        element_set_no=None,
        rev_at_epoch=None,
        bstar=0.0,
        mean_motion_dot=0.0,
        mean_motion_ddot=0.0,
    )
    fp = compute_record_fingerprint(rec)
    assert len(fp) == 64


def test_fingerprint_creation_date_utc_normalization() -> None:
    """Equivalent UTC timestamps for creation_date yield identical fingerprints."""
    rec1 = make_history_record(
        gp_id=None,
        epoch="2024-01-01T00:00:00Z",
        creation_date="2024-01-01T01:00:00Z",
    )
    rec2 = make_history_record(
        gp_id=None,
        epoch="2024-01-01T00:00:00Z",
        creation_date="2024-01-01 01:00:00Z",
    )
    rec3 = make_history_record(
        gp_id=None,
        epoch="2024-01-01T00:00:00Z",
        creation_date="2024-01-01T01:00:00+00:00",
    )
    fp1 = compute_record_fingerprint(rec1)
    fp2 = compute_record_fingerprint(rec2)
    fp3 = compute_record_fingerprint(rec3)
    assert fp1 == fp2 == fp3


def test_fingerprint_source_override_distinguishes_non_gpid(memory_storage: OrbitStorage) -> None:
    """Same orbital record inserted with two different explicit source overrides must have distinct non-GP_ID fingerprints."""
    rec = make_history_record(gp_id=None, source="source_default")

    # Insert with first source override
    res1 = memory_storage.insert_history_record(rec, source="override_alpha")
    assert res1 is True
    fp_alpha = rec.fingerprint

    # Same record inserted with second source override
    res2 = memory_storage.insert_history_record(rec, source="override_beta")
    assert res2 is True
    fp_beta = rec.fingerprint

    assert fp_alpha != fp_beta
    assert memory_storage.count_history_records() == 2


def test_fingerprint_effective_source_equivalence() -> None:
    """Same effective source supplied through record.source and explicit argument must produce the same fingerprint."""
    rec1 = make_history_record(gp_id=None, source="space-track-archive")
    rec2 = make_history_record(gp_id=None, source="other-placeholder")

    fp_from_rec = compute_record_fingerprint(rec1)
    fp_from_arg = compute_record_fingerprint(rec2, source="space-track-archive")

    assert fp_from_rec == fp_from_arg


def test_fingerprint_gpid_unaffected_by_source_override() -> None:
    """GP_ID records retain identical gpid-based identity regardless of source override."""
    rec = make_history_record(gp_id=45678, source="source_original")

    fp_default = compute_record_fingerprint(rec)
    fp_override = compute_record_fingerprint(rec, source="source_custom_override")

    assert fp_default == fp_override == "gpid:45678"


# ---------------------------------------------------------------------------
# 2. Schema and Single Record Insertion Tests
# ---------------------------------------------------------------------------


def test_insert_history_record_distinct_gp_ids(memory_storage: OrbitStorage) -> None:
    """Distinct GP_IDs can be inserted and each receives a unique surrogate database ID."""
    rec1 = make_history_record(gp_id=101, epoch="2024-01-01T00:00:00Z")
    rec2 = make_history_record(gp_id=102, epoch="2024-01-02T00:00:00Z")

    res1 = memory_storage.insert_history_record(rec1)
    res2 = memory_storage.insert_history_record(rec2)

    assert res1 is True
    assert res2 is True
    assert rec1.id is not None
    assert rec2.id is not None
    assert rec1.id != rec2.id
    assert memory_storage.count_history_records() == 2


def test_insert_history_record_duplicate_gp_id_rejected(memory_storage: OrbitStorage) -> None:
    """Inserting a record with an already-existing GP_ID is safely ignored (returns False)."""
    rec1 = make_history_record(gp_id=201, epoch="2024-01-01T00:00:00Z")
    rec2 = make_history_record(gp_id=201, epoch="2024-01-02T00:00:00Z", mean_motion=15.7)

    res1 = memory_storage.insert_history_record(rec1)
    res2 = memory_storage.insert_history_record(rec2)

    assert res1 is True
    assert res2 is False
    assert memory_storage.count_history_records() == 1

    stored = memory_storage.get_history_record_by_gp_id(201)
    assert stored is not None
    assert stored.epoch == "2024-01-01T00:00:00Z"


def test_insert_history_record_same_epoch_distinct_element_sets_preserved(
    memory_storage: OrbitStorage,
) -> None:
    """Multiple element sets for the same satellite and exact same epoch are preserved if distinct."""
    # Two distinct issuances for the same epoch (e.g. updated fit from tracking network)
    rec1 = make_history_record(
        norad_cat_id=25544,
        epoch="2024-01-01T00:00:00Z",
        creation_date="2024-01-01T01:00:00Z",
        gp_id=301,
        mean_motion=15.50000000,
    )
    rec2 = make_history_record(
        norad_cat_id=25544,
        epoch="2024-01-01T00:00:00Z",
        creation_date="2024-01-01T02:00:00Z",
        gp_id=302,
        mean_motion=15.50000100,
    )

    res1 = memory_storage.insert_history_record(rec1)
    res2 = memory_storage.insert_history_record(rec2)

    assert res1 is True
    assert res2 is True
    assert memory_storage.count_history_records(norad_cat_id=25544) == 2


def test_insert_history_non_spacetrack_without_creation_date(
    memory_storage: OrbitStorage,
) -> None:
    """Non-Space-Track records (e.g. CelesTrak) without gp_id or creation_date are stored cleanly."""
    rec1 = make_history_record(
        norad_cat_id=25544,
        source="celestrak",
        gp_id=None,
        creation_date=None,
        epoch="2024-01-01T00:00:00Z",
    )
    rec2 = make_history_record(
        norad_cat_id=25544,
        source="celestrak",
        gp_id=None,
        creation_date=None,
        epoch="2024-01-02T00:00:00Z",
    )

    res1 = memory_storage.insert_history_record(rec1)
    res2 = memory_storage.insert_history_record(rec2)

    assert res1 is True
    assert res2 is True
    assert memory_storage.count_history_records(norad_cat_id=25544) == 2


def test_fingerprint_duplicate_detection(memory_storage: OrbitStorage) -> None:
    """Re-inserting the exact same record without gp_id is detected via fingerprint and ignored."""
    rec1 = make_history_record(
        norad_cat_id=25544,
        source="celestrak",
        gp_id=None,
        creation_date=None,
        epoch="2024-01-01T00:00:00Z",
    )
    rec2 = make_history_record(
        norad_cat_id=25544,
        source="celestrak",
        gp_id=None,
        creation_date=None,
        epoch="2024-01-01T00:00:00Z",
    )

    res1 = memory_storage.insert_history_record(rec1)
    res2 = memory_storage.insert_history_record(rec2)

    assert res1 is True
    assert res2 is False
    assert memory_storage.count_history_records() == 1


# ---------------------------------------------------------------------------
# 3. Batch Insertion and Rollback Tests
# ---------------------------------------------------------------------------


def test_batch_insert_history_success_and_deduplication(memory_storage: OrbitStorage) -> None:
    """batch_insert_history correctly inserts records and skips intra-batch and pre-existing duplicates."""
    # Pre-insert one record
    pre_rec = make_history_record(gp_id=501, epoch="2024-01-01T00:00:00Z")
    memory_storage.insert_history_record(pre_rec)

    records = [
        make_history_record(gp_id=501, epoch="2024-01-01T00:00:00Z"),  # Pre-existing duplicate
        make_history_record(gp_id=502, epoch="2024-01-02T00:00:00Z"),  # New
        make_history_record(gp_id=503, epoch="2024-01-03T00:00:00Z"),  # New
        make_history_record(gp_id=502, epoch="2024-01-02T00:00:00Z"),  # Intra-batch duplicate
    ]

    inserted, skipped = memory_storage.batch_insert_history(records)
    assert inserted == 2
    assert skipped == 2
    assert memory_storage.count_history_records() == 3


def test_batch_insert_history_empty(memory_storage: OrbitStorage) -> None:
    """batch_insert_history with an empty list returns (0, 0)."""
    assert memory_storage.batch_insert_history([]) == (0, 0)


def test_batch_insert_history_atomic_rollback_on_failure(memory_storage: OrbitStorage) -> None:
    """If an invalid record causes an exception during batch insert, all batch changes are rolled back."""
    records = [
        make_history_record(gp_id=601, epoch="2024-01-01T00:00:00Z"),
        make_history_record(gp_id=602, epoch="2024-01-02T00:00:00Z"),
        make_history_record(gp_id=603, epoch="invalid-unparseable-epoch"),
    ]

    with pytest.raises(RecordValidationError):
        memory_storage.batch_insert_history(records)

    # Verify no records from the batch were committed
    assert memory_storage.count_history_records() == 0


# ---------------------------------------------------------------------------
# 4. Chronological Range Query Tests
# ---------------------------------------------------------------------------


def test_query_history_range_chronological_ordering(memory_storage: OrbitStorage) -> None:
    """query_history_range returns records strictly ordered by epoch_timestamp ascending."""
    # Insert records out of chronological order
    recs = [
        make_history_record(norad_cat_id=25544, gp_id=703, epoch="2024-01-03T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=701, epoch="2024-01-01T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=702, epoch="2024-01-02T00:00:00Z"),
        make_history_record(norad_cat_id=99999, gp_id=704, epoch="2024-01-01T12:00:00Z"),  # Other satellite
    ]
    memory_storage.batch_insert_history(recs)

    results = memory_storage.query_history_range(norad_cat_id=25544)
    assert len(results) == 3
    assert [r.gp_id for r in results] == [701, 702, 703]
    assert [r.epoch for r in results] == [
        "2024-01-01T00:00:00Z",
        "2024-01-02T00:00:00Z",
        "2024-01-03T00:00:00Z",
    ]


def test_query_history_range_with_start_and_end_filters(memory_storage: OrbitStorage) -> None:
    """query_history_range filters properly by start_epoch and end_epoch boundaries."""
    recs = [
        make_history_record(norad_cat_id=25544, gp_id=801, epoch="2024-01-01T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=802, epoch="2024-01-02T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=803, epoch="2024-01-03T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=804, epoch="2024-01-04T00:00:00Z"),
        make_history_record(norad_cat_id=25544, gp_id=805, epoch="2024-01-05T00:00:00Z"),
    ]
    memory_storage.batch_insert_history(recs)

    # Filter with start_epoch only
    res_start = memory_storage.query_history_range(
        norad_cat_id=25544, start_epoch="2024-01-03T00:00:00Z"
    )
    assert [r.gp_id for r in res_start] == [803, 804, 805]

    # Filter with end_epoch only
    res_end = memory_storage.query_history_range(
        norad_cat_id=25544, end_epoch="2024-01-02T00:00:00Z"
    )
    assert [r.gp_id for r in res_end] == [801, 802]

    # Filter with both start and end
    res_both = memory_storage.query_history_range(
        norad_cat_id=25544,
        start_epoch="2024-01-02T00:00:00Z",
        end_epoch="2024-01-04T00:00:00Z",
    )
    assert [r.gp_id for r in res_both] == [802, 803, 804]


def test_query_history_range_pagination(memory_storage: OrbitStorage) -> None:
    """query_history_range respects limit and offset parameters."""
    recs = [
        make_history_record(norad_cat_id=25544, gp_id=900 + i, epoch=f"2024-01-0{i}T00:00:00Z")
        for i in range(1, 6)
    ]
    memory_storage.batch_insert_history(recs)

    page1 = memory_storage.query_history_range(norad_cat_id=25544, limit=2, offset=0)
    assert [r.gp_id for r in page1] == [901, 902]

    page2 = memory_storage.query_history_range(norad_cat_id=25544, limit=2, offset=2)
    assert [r.gp_id for r in page2] == [903, 904]

    page3 = memory_storage.query_history_range(norad_cat_id=25544, limit=2, offset=4)
    assert [r.gp_id for r in page3] == [905]


# ---------------------------------------------------------------------------
# 5. Retrieval by ID and Metadata Preservation
# ---------------------------------------------------------------------------


def test_get_history_record_by_id_and_gp_id(memory_storage: OrbitStorage) -> None:
    """Historical records can be fetched by database ID and Space-Track GP_ID."""
    rec = make_history_record(
        gp_id=11111,
        semimajor_axis=6791.5,
        period=92.9,
        apoapsis=425.2,
        periapsis=408.8,
        decay_date="2030-01-01T00:00:00Z",
        tle_line1="1 25544U 98067A   24001.00000000  .00002000  00000-0  10000-4 0  9991",
        tle_line2="2 25544  51.6400 120.0000 0001000  60.0000  30.0000 15.50000000 10002",
    )
    memory_storage.insert_history_record(rec)

    # Lookup by GP_ID
    by_gp = memory_storage.get_history_record_by_gp_id(11111)
    assert by_gp is not None
    assert by_gp.norad_cat_id == 25544
    assert by_gp.semimajor_axis == 6791.5
    assert by_gp.period == 92.9
    assert by_gp.apoapsis == 425.2
    assert by_gp.periapsis == 408.8
    assert by_gp.decay_date == "2030-01-01T00:00:00Z"
    assert by_gp.tle_line1 is not None
    assert by_gp.tle_line2 is not None

    # Lookup by ID
    by_id = memory_storage.get_history_record_by_id(rec.id)
    assert by_id is not None
    assert by_id.gp_id == 11111
    assert by_id.fingerprint == "gpid:11111"

    # Non-existent lookups
    assert memory_storage.get_history_record_by_id(999999) is None
    assert memory_storage.get_history_record_by_gp_id(999999) is None


# ---------------------------------------------------------------------------
# 6. Ingestion Checkpoints Tests
# ---------------------------------------------------------------------------


def test_checkpoint_crud_lifecycle(memory_storage: OrbitStorage) -> None:
    """Ingestion checkpoints can be saved, updated, queried, and filtered."""
    # 1. Initial pending checkpoint
    cp1 = IngestionCheckpoint(
        norad_cat_id=25544,
        source="space-track",
        status="in_progress",
        earliest_epoch="2020-01-01T00:00:00Z",
        latest_epoch="2021-01-01T00:00:00Z",
        record_count=500,
        last_attempt_at="2024-01-01T10:00:00Z",
    )
    memory_storage.save_checkpoint(cp1)

    fetched = memory_storage.get_checkpoint(25544, "space-track")
    assert fetched is not None
    assert fetched.status == "in_progress"
    assert fetched.record_count == 500
    assert fetched.earliest_epoch_timestamp is not None
    assert fetched.latest_epoch_timestamp is not None
    assert fetched.completed_at is None

    # 2. Update existing checkpoint to completed
    cp1.status = "completed"
    cp1.latest_epoch = "2024-01-01T00:00:00Z"
    cp1.record_count = 1500
    cp1.completed_at = "2024-01-01T11:00:00Z"
    memory_storage.update_checkpoint(cp1)

    updated = memory_storage.get_checkpoint(25544, "space-track")
    assert updated is not None
    assert updated.status == "completed"
    assert updated.record_count == 1500
    assert updated.completed_at == "2024-01-01T11:00:00Z"

    # 3. Add second checkpoint for a different satellite
    cp2 = IngestionCheckpoint(
        norad_cat_id=43205,
        source="space-track",
        status="failed",
        last_error="Network error during archive stream",
        last_attempt_at="2024-01-01T12:00:00Z",
    )
    memory_storage.save_checkpoint(cp2)

    # 4. List and filter
    all_cps = memory_storage.list_checkpoints()
    assert len(all_cps) == 2

    failed_cps = memory_storage.list_checkpoints(status="failed")
    assert len(failed_cps) == 1
    assert failed_cps[0].norad_cat_id == 43205

    # 5. Clear checkpoints
    memory_storage.clear_checkpoints()
    assert memory_storage.list_checkpoints() == []


def test_checkpoint_cumulative_bounds_preservation(memory_storage: OrbitStorage) -> None:
    """UPSERT_CHECKPOINT_SQL preserves true cumulative min/max epoch bounds and timestamp pairing."""
    # 1. First checkpoint insertion with missing bounds
    cp_init_missing = IngestionCheckpoint(
        norad_cat_id=12345,
        source="archive_test",
        status="pending",
        last_attempt_at="2024-01-01T00:00:00Z",
    )
    memory_storage.save_checkpoint(cp_init_missing)

    cp0 = memory_storage.get_checkpoint(12345, "archive_test")
    assert cp0 is not None
    assert cp0.earliest_epoch is None
    assert cp0.earliest_epoch_timestamp is None
    assert cp0.latest_epoch is None
    assert cp0.latest_epoch_timestamp is None

    # 2. Update with initial valid bounds [2022-01-01, 2023-01-01]
    cp_with_bounds = IngestionCheckpoint(
        norad_cat_id=12345,
        source="archive_test",
        status="in_progress",
        earliest_epoch="2022-01-01T00:00:00Z",
        latest_epoch="2023-01-01T00:00:00Z",
        record_count=100,
        last_attempt_at="2024-01-01T01:00:00Z",
    )
    memory_storage.save_checkpoint(cp_with_bounds)

    cp1 = memory_storage.get_checkpoint(12345, "archive_test")
    assert cp1 is not None
    assert cp1.earliest_epoch == "2022-01-01T00:00:00Z"
    assert cp1.latest_epoch == "2023-01-01T00:00:00Z"
    assert cp1.earliest_epoch_timestamp == parse_epoch_timestamp("2022-01-01T00:00:00Z")
    assert cp1.latest_epoch_timestamp == parse_epoch_timestamp("2023-01-01T00:00:00Z")

    # 3. Existing bounds followed by checkpoint containing newer bounds:
    # Incoming earliest is 2022-06-01 (newer than 2022-01-01), latest is 2024-01-01 (newer than 2023-01-01)
    # Earliest must retain MIN (2022-01-01), Latest must retain MAX (2024-01-01)
    cp_newer = IngestionCheckpoint(
        norad_cat_id=12345,
        source="archive_test",
        status="in_progress",
        earliest_epoch="2022-06-01T00:00:00Z",
        latest_epoch="2024-01-01T00:00:00Z",
        record_count=200,
        last_attempt_at="2024-01-01T02:00:00Z",
    )
    memory_storage.save_checkpoint(cp_newer)

    cp2 = memory_storage.get_checkpoint(12345, "archive_test")
    assert cp2 is not None
    assert cp2.earliest_epoch == "2022-01-01T00:00:00Z"
    assert cp2.latest_epoch == "2024-01-01T00:00:00Z"
    assert cp2.earliest_epoch_timestamp == parse_epoch_timestamp("2022-01-01T00:00:00Z")
    assert cp2.latest_epoch_timestamp == parse_epoch_timestamp("2024-01-01T00:00:00Z")

    # 4. Existing bounds followed by checkpoint containing older bounds:
    # Incoming earliest is 2020-01-01 (older than 2022-01-01), latest is 2023-06-01 (older than 2024-01-01)
    # Earliest must expand to MIN (2020-01-01), Latest must retain MAX (2024-01-01)
    cp_older = IngestionCheckpoint(
        norad_cat_id=12345,
        source="archive_test",
        status="in_progress",
        earliest_epoch="2020-01-01T00:00:00Z",
        latest_epoch="2023-06-01T00:00:00Z",
        record_count=300,
        last_attempt_at="2024-01-01T03:00:00Z",
    )
    memory_storage.save_checkpoint(cp_older)

    cp3 = memory_storage.get_checkpoint(12345, "archive_test")
    assert cp3 is not None
    assert cp3.earliest_epoch == "2020-01-01T00:00:00Z"
    assert cp3.latest_epoch == "2024-01-01T00:00:00Z"
    assert cp3.earliest_epoch_timestamp == parse_epoch_timestamp("2020-01-01T00:00:00Z")
    assert cp3.latest_epoch_timestamp == parse_epoch_timestamp("2024-01-01T00:00:00Z")

    # 5. NULL incoming bounds: neither earliest nor latest is specified
    # Must preserve existing cumulative bounds intact
    cp_null_incoming = IngestionCheckpoint(
        norad_cat_id=12345,
        source="archive_test",
        status="completed",
        earliest_epoch=None,
        latest_epoch=None,
        record_count=350,
        last_attempt_at="2024-01-01T04:00:00Z",
        completed_at="2024-01-01T04:00:00Z",
    )
    memory_storage.save_checkpoint(cp_null_incoming)

    cp4 = memory_storage.get_checkpoint(12345, "archive_test")
    assert cp4 is not None
    assert cp4.earliest_epoch == "2020-01-01T00:00:00Z"
    assert cp4.latest_epoch == "2024-01-01T00:00:00Z"
    assert cp4.record_count == 350
    assert cp4.status == "completed"
    assert cp4.completed_at == "2024-01-01T04:00:00Z"

    # 6. Correct correspondence between epoch strings and numeric timestamps
    assert cp4.earliest_epoch_timestamp == parse_epoch_timestamp(cp4.earliest_epoch)
    assert cp4.latest_epoch_timestamp == parse_epoch_timestamp(cp4.latest_epoch)


# ---------------------------------------------------------------------------
# 7. Isolation and Regression Tests
# ---------------------------------------------------------------------------


def test_historical_storage_isolates_from_latest_records(memory_storage: OrbitStorage) -> None:
    """Historical records do not modify or interfere with the primary orbital_records table."""
    # Insert primary latest record
    latest = OrbitalRecord(
        norad_cat_id=25544,
        object_name="ISS LATEST",
        object_id="1998-067A",
        epoch="2024-01-05T00:00:00Z",
        mean_motion=15.5,
        eccentricity=0.0001,
        inclination=51.6,
        ra_of_asc_node=100.0,
        arg_of_pericenter=50.0,
        mean_anomaly=20.0,
    )
    memory_storage.upsert(latest)

    # Insert historical records with older epochs
    hist1 = make_history_record(norad_cat_id=25544, gp_id=1001, epoch="2024-01-01T00:00:00Z")
    hist2 = make_history_record(norad_cat_id=25544, gp_id=1002, epoch="2024-01-02T00:00:00Z")
    memory_storage.insert_history_record(hist1)
    memory_storage.insert_history_record(hist2)

    # Verify primary record is untouched
    primary = memory_storage.get(25544)
    assert primary is not None
    assert primary.epoch == "2024-01-05T00:00:00Z"
    assert primary.object_name == "ISS LATEST"
    assert memory_storage.count() == 1
    assert memory_storage.count_history_records() == 2

    # Clear historical table; primary record must remain
    memory_storage.clear_history()
    assert memory_storage.count_history_records() == 0
    assert memory_storage.count() == 1
    assert memory_storage.get(25544) is not None

    # Clear primary table; historical operations unaffected
    memory_storage.insert_history_record(hist1)
    memory_storage.clear()
    assert memory_storage.count() == 0
    assert memory_storage.count_history_records() == 1


def test_legacy_database_initialization_and_compatibility() -> None:
    """OrbitStorage safely opens and upgrades pre-1.6.1 database files without data loss."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "legacy.db"

        # 1. Manually create pre-1.6.1 database with only orbital_records
        conn = sqlite3.connect(db_file)
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_EPOCH_SQL)
        conn.execute(CREATE_INDEX_NAME_SQL)
        conn.execute("""
            INSERT INTO orbital_records (
                norad_cat_id, object_name, object_id, epoch, epoch_timestamp,
                mean_motion, eccentricity, inclination, ra_of_asc_node, arg_of_pericenter,
                mean_anomaly, ephemeris_type, classification_type, element_set_no,
                rev_at_epoch, bstar, mean_motion_dot, mean_motion_ddot, source, retrieved_at
            ) VALUES (
                25544, 'ISS (ZARYA)', '1998-067A', '2024-01-01T00:00:00Z', 1704067200.0,
                15.5, 0.0001, 51.64, 120.0, 60.0, 30.0, 0, 'U', 999, 1000,
                0.00002, 0.00001, 0.0, 'celestrak', '2024-01-01T01:00:00Z'
            )
        """)
        conn.commit()
        conn.close()

        # 2. Open with OrbitStorage, triggering _init_db()
        storage = OrbitStorage(db_url=f"sqlite:///{db_file}")

        # Verify legacy data intact
        rec = storage.get(25544)
        assert rec is not None
        assert rec.norad_cat_id == 25544
        assert rec.object_name == "ISS (ZARYA)"

        # Verify new tables exist and can store history
        hist = make_history_record(norad_cat_id=25544, gp_id=9999, epoch="2024-01-02T00:00:00Z")
        assert storage.insert_history_record(hist) is True
        assert storage.count_history_records() == 1
        assert storage.count() == 1

        # Idempotency check: re-run initialization
        storage._init_db()
        assert storage.get(25544) is not None
        assert storage.count_history_records() == 1

        storage.close()
