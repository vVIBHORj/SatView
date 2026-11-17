"""Comprehensive deterministic tests for historical archive reader and ingestion workflow."""

import csv
import io
import json
from pathlib import Path
import tempfile
import zipfile

import pytest

from app.orbit.archive import (
    ArchiveFormatError,
    ArchiveParseError,
    ArchiveSecurityError,
    HistoricalArchiveIngestor,
    HistoricalArchiveReader,
    IngestionResult,
    clean_record_dict,
    validate_zip_entry_safety,
)
from app.orbit.exceptions import DatabaseError, RecordValidationError
from app.orbit.models import HistoricalOrbitalRecord, OrbitalRecord
from app.orbit.storage import OrbitStorage, parse_epoch_timestamp


@pytest.fixture
def memory_storage() -> OrbitStorage:
    """Fixture providing an isolated in-memory OrbitStorage instance."""
    storage = OrbitStorage(db_url=":memory:")
    yield storage
    storage.close()


@pytest.fixture
def reader() -> HistoricalArchiveReader:
    """Fixture providing a HistoricalArchiveReader instance."""
    return HistoricalArchiveReader()


@pytest.fixture
def ingestor(memory_storage: OrbitStorage) -> HistoricalArchiveIngestor:
    """Fixture providing a HistoricalArchiveIngestor with in-memory storage."""
    return HistoricalArchiveIngestor(storage=memory_storage)


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


def make_raw_dict(
    norad_cat_id: int = 25544,
    object_name: str = "ISS (ZARYA)",
    object_id: str = "1998-067A",
    epoch: str = "2023-01-01T00:00:00Z",
    mean_motion: float = 15.5,
    eccentricity: float = 0.0001,
    inclination: float = 51.64,
    ra_of_asc_node: float = 120.0,
    arg_of_pericenter: float = 60.0,
    mean_anomaly: float = 30.0,
    gp_id: int | None = 10001,
    creation_date: str | None = "2023-01-01T01:00:00Z",
    bstar: float = 0.00002,
    mean_motion_dot: float = 0.00001,
    mean_motion_ddot: float = 0.0,
    semimajor_axis: float | None = 6780.0,
    period: float | None = 92.8,
    apoapsis: float | None = 420.0,
    periapsis: float | None = 410.0,
    decay_date: str | None = None,
    tle_line1: str | None = "1 25544U 98067A   23001.00000000  .00001000  00000-0  20000-4 0  9991",
    tle_line2: str | None = "2 25544  51.6400 120.0000 0001000  60.0000  30.0000 15.50000000 10002",
) -> dict:
    return {
        "NORAD_CAT_ID": norad_cat_id,
        "OBJECT_NAME": object_name,
        "OBJECT_ID": object_id,
        "EPOCH": epoch,
        "MEAN_MOTION": mean_motion,
        "ECCENTRICITY": eccentricity,
        "INCLINATION": inclination,
        "RA_OF_ASC_NODE": ra_of_asc_node,
        "ARG_OF_PERICENTER": arg_of_pericenter,
        "MEAN_ANOMALY": mean_anomaly,
        "GP_ID": gp_id,
        "CREATION_DATE": creation_date,
        "BSTAR": bstar,
        "MEAN_MOTION_DOT": mean_motion_dot,
        "MEAN_MOTION_DDOT": mean_motion_ddot,
        "SEMIMAJOR_AXIS": semimajor_axis,
        "PERIOD": period,
        "APOAPSIS": apoapsis,
        "PERIAPSIS": periapsis,
        "DECAY_DATE": decay_date,
        "TLE_LINE1": tle_line1,
        "TLE_LINE2": tle_line2,
    }


# ---------------------------------------------------------------------------
# 1. Archive Reader Unit Tests: JSON Parsing
# ---------------------------------------------------------------------------


def test_reader_json_array_parsing(reader: HistoricalArchiveReader) -> None:
    """Reader parses standard Space-Track JSON array exports with strings and numbers."""
    records_data = [
        make_raw_dict(gp_id=101, epoch="2023-01-01T00:00:00Z"),
        make_raw_dict(gp_id=102, epoch="2023-01-02T00:00:00Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records_data, f)
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(errs) == 0
        assert len(recs) == 2
        assert recs[0].gp_id == 101
        assert recs[1].gp_id == 102
        assert recs[0].epoch == "2023-01-01T00:00:00Z"
    finally:
        temp_path.unlink(missing_ok=True)


def test_reader_single_json_object(reader: HistoricalArchiveReader) -> None:
    """Reader parses single standalone Space-Track JSON record objects."""
    record_data = make_raw_dict(gp_id=201)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(record_data, f)
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(errs) == 0
        assert len(recs) == 1
        assert recs[0].gp_id == 201
    finally:
        temp_path.unlink(missing_ok=True)


def test_reader_ndjson_lines(reader: HistoricalArchiveReader) -> None:
    """Reader parses newline-delimited JSON (NDJSON/JSON Lines) streams."""
    lines = [
        json.dumps(make_raw_dict(gp_id=301, epoch="2023-01-01T00:00:00Z")),
        json.dumps(make_raw_dict(gp_id=302, epoch="2023-01-02T00:00:00Z")),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines))
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(errs) == 0
        assert len(recs) == 2
        assert recs[0].gp_id == 301
        assert recs[1].gp_id == 302
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Archive Reader Unit Tests: CSV Parsing
# ---------------------------------------------------------------------------


def test_reader_csv_parsing(reader: HistoricalArchiveReader) -> None:
    """Reader parses Space-Track CSV exports with string-encoded types and empty nulls."""
    rows = [
        make_raw_dict(gp_id=401, epoch="2023-01-01 00:00:00", decay_date=""),
        make_raw_dict(gp_id=402, epoch="2023-01-02 00:00:00", decay_date="2030-01-01"),
    ]
    fieldnames = list(rows[0].keys())

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(errs) == 0
        assert len(recs) == 2
        assert recs[0].gp_id == 401
        assert recs[0].epoch == "2023-01-01T00:00:00Z"
        assert recs[0].decay_date is None
        assert recs[1].gp_id == 402
        assert recs[1].decay_date == "2030-01-01"
    finally:
        temp_path.unlink(missing_ok=True)


def test_reader_csv_case_insensitive_headers(reader: HistoricalArchiveReader) -> None:
    """Reader handles lowercase or mixed-case CSV headers seamlessly."""
    row = {
        "norad_cat_id": "25544",
        "object_name": "ISS",
        "object_id": "1998-067A",
        "epoch": "2023-01-01T00:00:00Z",
        "mean_motion": "15.5",
        "eccentricity": "0.0001",
        "inclination": "51.64",
        "ra_of_asc_node": "120.0",
        "arg_of_pericenter": "60.0",
        "mean_anomaly": "30.0",
        "gp_id": "501",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(errs) == 0
        assert len(recs) == 1
        assert recs[0].gp_id == 501
        assert recs[0].norad_cat_id == 25544
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Archive Reader Unit Tests: ZIP Archives & Multiple Entries
# ---------------------------------------------------------------------------


def test_reader_zip_multiple_entries(reader: HistoricalArchiveReader) -> None:
    """Reader iterates through multiple CSV and JSON entries in nested folders inside a ZIP."""
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "bulk_archive.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            # Entry 1: CSV in subfolder
            csv_buf = io.StringIO()
            w = csv.DictWriter(csv_buf, fieldnames=["NORAD_CAT_ID", "OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY", "GP_ID"])
            w.writeheader()
            w.writerow({"NORAD_CAT_ID": 25544, "OBJECT_NAME": "ISS", "OBJECT_ID": "1998-067A", "EPOCH": "2023-01-01T00:00:00Z", "MEAN_MOTION": 15.5, "ECCENTRICITY": 0.0001, "INCLINATION": 51.64, "RA_OF_ASC_NODE": 120.0, "ARG_OF_PERICENTER": 60.0, "MEAN_ANOMALY": 30.0, "GP_ID": 601})
            zf.writestr("sub/iss.csv", csv_buf.getvalue())

            # Entry 2: JSON in root
            json_data = [
                {"NORAD_CAT_ID": 43205, "OBJECT_NAME": "CSS", "OBJECT_ID": "2021-035A", "EPOCH": "2023-01-01T00:00:00Z", "MEAN_MOTION": 15.6, "ECCENTRICITY": 0.0002, "INCLINATION": 41.5, "RA_OF_ASC_NODE": 80.0, "ARG_OF_PERICENTER": 45.0, "MEAN_ANOMALY": 15.0, "GP_ID": 602}
            ]
            zf.writestr("css.json", json.dumps(json_data))

            # Entry 3: Unrelated non-data file (ignored)
            zf.writestr("README.txt", "Space track archive description")

        recs, errs = reader.parse_archive(zip_path)
        assert len(errs) == 0
        assert len(recs) == 2
        assert {r.norad_cat_id for r in recs} == {25544, 43205}
        assert {r.gp_id for r in recs} == {601, 602}


# ---------------------------------------------------------------------------
# 4. Error Handling and Context Tests
# ---------------------------------------------------------------------------


def test_reader_invalid_record_structured_error_context(reader: HistoricalArchiveReader) -> None:
    """Malformed records produce structured ArchiveParseError identifying entry, row, and reason."""
    rows = [
        make_raw_dict(gp_id=701, epoch="2023-01-01T00:00:00Z"),
        {"NORAD_CAT_ID": "INVALID_CAT", "OBJECT_NAME": "BAD", "OBJECT_ID": "X", "EPOCH": "2023-01-02T00:00:00Z", "MEAN_MOTION": "not_a_float", "ECCENTRICITY": 0.1, "INCLINATION": 50.0, "RA_OF_ASC_NODE": 0.0, "ARG_OF_PERICENTER": 0.0, "MEAN_ANOMALY": 0.0},
        make_raw_dict(gp_id=702, epoch="2023-01-03T00:00:00Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        temp_path = Path(f.name)

    try:
        recs, errs = reader.parse_archive(temp_path)
        assert len(recs) == 2
        assert len(errs) == 1
        err = errs[0]
        assert err.entry_name == temp_path.name
        assert err.row_number == 3  # Header is line 1, row 1 is line 2, bad row is line 3
        assert err.raw_record is not None
        assert err.raw_record["NORAD_CAT_ID"] == "INVALID_CAT"
        assert "validation error" in err.error_message.lower() or "float" in err.error_message.lower()
    finally:
        temp_path.unlink(missing_ok=True)


def test_reader_empty_and_unsupported_archives(reader: HistoricalArchiveReader) -> None:
    """Reader rejects empty files, unsupported extensions, and empty zips with ArchiveFormatError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. 0-byte file
        empty_file = Path(tmpdir) / "empty.csv"
        empty_file.write_text("")
        with pytest.raises(ArchiveFormatError, match="empty"):
            reader.parse_archive(empty_file)

        # 2. Unsupported extension
        unsupported = Path(tmpdir) / "data.tar.gz"
        unsupported.write_text("dummy")
        with pytest.raises(ArchiveFormatError, match="Unsupported archive format"):
            reader.parse_archive(unsupported)

        # 3. Empty ZIP
        empty_zip = Path(tmpdir) / "empty.zip"
        with zipfile.ZipFile(empty_zip, "w") as zf:
            pass
        with pytest.raises(ArchiveFormatError, match="empty"):
            reader.parse_archive(empty_zip)

        # 4. ZIP without supported entries
        txt_zip = Path(tmpdir) / "txt_only.zip"
        with zipfile.ZipFile(txt_zip, "w") as zf:
            zf.writestr("info.txt", "hello")
        with pytest.raises(ArchiveFormatError, match="no supported"):
            reader.parse_archive(txt_zip)


def test_reader_unsafe_zip_paths_rejected(reader: HistoricalArchiveReader) -> None:
    """ZIP entries attempting directory traversal or absolute escaping raise ArchiveSecurityError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        unsafe_zip = Path(tmpdir) / "zip_slip.zip"
        with zipfile.ZipFile(unsafe_zip, "w") as zf:
            zf.writestr("../traversal.csv", "dummy")

        with pytest.raises(ArchiveSecurityError, match="path traversal"):
            reader.parse_archive(unsafe_zip)


# ---------------------------------------------------------------------------
# 5. Ingestion Workflow Tests
# ---------------------------------------------------------------------------


def test_ingest_archive_success(ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage) -> None:
    """Ingestor processes archive, inserts records, and marks checkpoints completed."""
    records_data = [
        make_raw_dict(norad_cat_id=25544, gp_id=1001, epoch="2023-01-01T00:00:00Z"),
        make_raw_dict(norad_cat_id=25544, gp_id=1002, epoch="2023-01-02T00:00:00Z"),
        make_raw_dict(norad_cat_id=43205, gp_id=2001, epoch="2023-01-01T12:00:00Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records_data, f)
        temp_path = Path(f.name)

    try:
        result = ingestor.ingest_archive(temp_path, source="space-track:test-2023")
        assert result.status == "completed"
        assert result.total_records_read == 3
        assert result.total_inserted == 3
        assert result.total_skipped == 0
        assert result.satellites_affected == [25544, 43205]

        # Verify database storage
        assert memory_storage.count_history_records(norad_cat_id=25544) == 2
        assert memory_storage.count_history_records(norad_cat_id=43205) == 1

        # Verify checkpoints
        cp_iss = memory_storage.get_checkpoint(25544, "space-track:test-2023")
        assert cp_iss is not None
        assert cp_iss.status == "completed"
        assert cp_iss.record_count == 2
        assert cp_iss.earliest_epoch == "2023-01-01T00:00:00Z"
        assert cp_iss.latest_epoch == "2023-01-02T00:00:00Z"
        assert cp_iss.completed_at is not None

        cp_css = memory_storage.get_checkpoint(43205, "space-track:test-2023")
        assert cp_css is not None
        assert cp_css.status == "completed"
        assert cp_css.record_count == 1
    finally:
        temp_path.unlink(missing_ok=True)


def test_ingest_archive_duplicate_deduplication(ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage) -> None:
    """Duplicate records within an archive are safely deduplicated and counted as skipped."""
    records_data = [
        make_raw_dict(norad_cat_id=25544, gp_id=3001, epoch="2023-01-01T00:00:00Z"),
        make_raw_dict(norad_cat_id=25544, gp_id=3001, epoch="2023-01-01T00:00:00Z"),  # Duplicate
        make_raw_dict(norad_cat_id=25544, gp_id=3002, epoch="2023-01-02T00:00:00Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records_data, f)
        temp_path = Path(f.name)

    try:
        result = ingestor.ingest_archive(temp_path, source="space-track:dedup-test")
        assert result.status == "completed"
        assert result.total_records_read == 3
        assert result.total_inserted == 2
        assert result.total_skipped == 1
        assert memory_storage.count_history_records(norad_cat_id=25544) == 2
    finally:
        temp_path.unlink(missing_ok=True)


def test_ingest_archive_same_epoch_distinct_records_preserved(
    ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage
) -> None:
    """Distinct element sets sharing the same epoch are both inserted and preserved."""
    records_data = [
        make_raw_dict(norad_cat_id=25544, gp_id=4001, epoch="2023-01-01T00:00:00Z", mean_motion=15.5000),
        make_raw_dict(norad_cat_id=25544, gp_id=4002, epoch="2023-01-01T00:00:00Z", mean_motion=15.5001),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records_data, f)
        temp_path = Path(f.name)

    try:
        result = ingestor.ingest_archive(temp_path, source="space-track:epoch-test")
        assert result.status == "completed"
        assert result.total_inserted == 2
        assert memory_storage.count_history_records(norad_cat_id=25544) == 2
    finally:
        temp_path.unlink(missing_ok=True)


def test_ingest_archive_interrupted_resume(ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage) -> None:
    """Interrupted ingestion can resume on the same archive without double-counting or losing bounds."""
    source_id = "space-track:resume-test"

    # Stage 1: Partial ingestion (first 2 records)
    records_stage1 = [
        make_raw_dict(norad_cat_id=25544, gp_id=5001, epoch="2023-01-01T00:00:00Z"),
        make_raw_dict(norad_cat_id=25544, gp_id=5002, epoch="2023-01-02T00:00:00Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f1:
        json.dump(records_stage1, f1)
        path1 = Path(f1.name)

    try:
        res1 = ingestor.ingest_archive(path1, source=source_id)
        assert res1.total_inserted == 2
        assert memory_storage.count_history_records(norad_cat_id=25544, source=source_id) == 2

        # Stage 2: Full archive (including previous 2 + 2 new records)
        records_stage2 = [
            make_raw_dict(norad_cat_id=25544, gp_id=5001, epoch="2023-01-01T00:00:00Z"),
            make_raw_dict(norad_cat_id=25544, gp_id=5002, epoch="2023-01-02T00:00:00Z"),
            make_raw_dict(norad_cat_id=25544, gp_id=5003, epoch="2023-01-03T00:00:00Z"),
            make_raw_dict(norad_cat_id=25544, gp_id=5004, epoch="2023-01-04T00:00:00Z"),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f2:
            json.dump(records_stage2, f2)
            path2 = Path(f2.name)

        try:
            res2 = ingestor.ingest_archive(path2, source=source_id)
            assert res2.status == "completed"
            assert res2.total_records_read == 4
            assert res2.total_inserted == 2  # 2 newly inserted
            assert res2.total_skipped == 2   # 2 skipped duplicates
            assert memory_storage.count_history_records(norad_cat_id=25544, source=source_id) == 4

            # Checkpoint verification
            cp = memory_storage.get_checkpoint(25544, source_id)
            assert cp is not None
            assert cp.status == "completed"
            assert cp.record_count == 4
            assert cp.earliest_epoch == "2023-01-01T00:00:00Z"
            assert cp.latest_epoch == "2023-01-04T00:00:00Z"
        finally:
            path2.unlink(missing_ok=True)
    finally:
        path1.unlink(missing_ok=True)


def test_ingest_archive_malformed_records_not_marked_completed(
    ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage
) -> None:
    """If an archive contains malformed rows, valid rows are stored but checkpoint is marked failed."""
    source_id = "space-track:partial-error"
    rows = [
        make_raw_dict(norad_cat_id=25544, gp_id=6001, epoch="2023-01-01T00:00:00Z"),
        {"NORAD_CAT_ID": "MALFORMED", "OBJECT_NAME": "X", "OBJECT_ID": "Y", "EPOCH": "BAD", "MEAN_MOTION": "NaN", "ECCENTRICITY": 0.0, "INCLINATION": 0.0, "RA_OF_ASC_NODE": 0.0, "ARG_OF_PERICENTER": 0.0, "MEAN_ANOMALY": 0.0},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        temp_path = Path(f.name)

    try:
        res = ingestor.ingest_archive(temp_path, source=source_id)
        assert res.status == "partial"
        assert res.total_inserted == 1
        assert len(res.errors) == 1

        # Checkpoint MUST be marked 'failed', NEVER 'completed'
        cp = memory_storage.get_checkpoint(25544, source_id)
        assert cp is not None
        assert cp.status == "failed"
        assert cp.last_error is not None
        assert "parse error" in cp.last_error
        assert cp.completed_at is None
    finally:
        temp_path.unlink(missing_ok=True)


def test_ingestion_isolates_from_latest_records(
    ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage
) -> None:
    """Historical archive ingestion does not modify the latest orbital_records cache."""
    # Pre-populate latest orbital_records
    latest_rec = OrbitalRecord(
        norad_cat_id=25544,
        object_name="ISS LATEST",
        object_id="1998-067A",
        epoch="2024-01-01T00:00:00Z",
        mean_motion=15.5,
        eccentricity=0.0001,
        inclination=51.64,
        ra_of_asc_node=120.0,
        arg_of_pericenter=60.0,
        mean_anomaly=30.0,
    )
    memory_storage.upsert(latest_rec)

    # Ingest historical archive
    records = [make_raw_dict(norad_cat_id=25544, gp_id=7001, epoch="2020-01-01T00:00:00Z")]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records, f)
        temp_path = Path(f.name)

    try:
        ingestor.ingest_archive(temp_path)

        # Primary table untouched
        stored_latest = memory_storage.get(25544)
        assert stored_latest is not None
        assert stored_latest.epoch == "2024-01-01T00:00:00Z"
        assert stored_latest.object_name == "ISS LATEST"
        assert memory_storage.count() == 1
        assert memory_storage.count_history_records() == 1
    finally:
        temp_path.unlink(missing_ok=True)


def test_ingest_archive_epoch_bounds_fractional_seconds_correctly_ordered(
    ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage
) -> None:
    """Checkpoint epoch bounds are ordered by numeric timestamp, not lexicographical string comparison."""
    # Note: '2023-01-01T00:00:00.500000Z' is lexicographically SMALLER than '2023-01-01T00:00:00Z'
    # due to '.' (46) < 'Z' (90), but chronologically LATER.
    source_id = "space-track:bounds-microsecond-test"
    records_data = [
        make_raw_dict(norad_cat_id=25544, gp_id=8001, epoch="2023-01-01T00:00:00Z"),
        make_raw_dict(norad_cat_id=25544, gp_id=8002, epoch="2023-01-01T00:00:00.500000Z"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records_data, f)
        temp_path = Path(f.name)

    try:
        res = ingestor.ingest_archive(temp_path, source=source_id)
        assert res.status == "completed"

        cp = memory_storage.get_checkpoint(25544, source_id)
        assert cp is not None
        assert cp.earliest_epoch == "2023-01-01T00:00:00Z"
        assert cp.latest_epoch == "2023-01-01T00:00:00.500000Z"
        assert cp.earliest_epoch_timestamp is not None
        assert cp.latest_epoch_timestamp is not None
        assert cp.earliest_epoch_timestamp < cp.latest_epoch_timestamp
    finally:
        temp_path.unlink(missing_ok=True)


def test_default_archive_source_disambiguates_different_files_with_same_stem(
    ingestor: HistoricalArchiveIngestor, memory_storage: OrbitStorage
) -> None:
    """Two different archives with the same filename stem receive distinct qualified sources via content digest."""
    with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
        path1 = Path(dir1) / "gp_history.json"
        path2 = Path(dir2) / "gp_history.json"

        # File 1: 2022 records
        records1 = [make_raw_dict(norad_cat_id=25544, gp_id=9001, epoch="2022-01-01T00:00:00Z")]
        path1.write_text(json.dumps(records1), encoding="utf-8")

        # File 2: 2023 records
        records2 = [make_raw_dict(norad_cat_id=25544, gp_id=9002, epoch="2023-01-01T00:00:00Z")]
        path2.write_text(json.dumps(records2), encoding="utf-8")

        res1 = ingestor.ingest_archive(path1)
        res2 = ingestor.ingest_archive(path2)

        # Sources must be distinct despite identical filename stem
        assert res1.source != res2.source
        assert res1.source.startswith("space-track:gp-history:gp_history:")
        assert res2.source.startswith("space-track:gp-history:gp_history:")

        # Both checkpoints exist independently
        cp1 = memory_storage.get_checkpoint(25544, res1.source)
        cp2 = memory_storage.get_checkpoint(25544, res2.source)
        assert cp1 is not None and cp1.earliest_epoch == "2022-01-01T00:00:00Z"
        assert cp2 is not None and cp2.earliest_epoch == "2023-01-01T00:00:00Z"


def test_zip_safety_backslash_path_traversal_rejected(reader: HistoricalArchiveReader) -> None:
    """Backslash-encoded path traversal inside ZIP entries is rejected across all platforms."""
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "backslash_traversal.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("sub\\..\\evil.csv", "dummy")

        with pytest.raises(ArchiveSecurityError, match="path traversal"):
            reader.parse_archive(zip_path)


def test_zip_oversized_uncompressed_entry_rejected(memory_storage: OrbitStorage) -> None:
    """ZIP entries exceeding uncompressed entry size limits raise ArchiveSecurityError."""
    custom_reader = HistoricalArchiveReader()
    custom_reader.MAX_UNCOMPRESSED_ENTRY_BYTES = 100  # Artificially low limit for testing

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "large_entry.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.csv", "x" * 200)

        with pytest.raises(ArchiveSecurityError, match="exceeds uncompressed size limit"):
            custom_reader.parse_archive(zip_path)
