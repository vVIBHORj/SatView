"""Tests for persistent orbital storage (Task 1.5)."""

from datetime import datetime, timezone
import concurrent.futures
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orbit.cache import TTLCache
from app.orbit.celestrak import CelesTrakClient
from app.orbit.exceptions import (
    CelesTrakError,
    CelesTrakRateLimitError,
    CelesTrakTimeoutError,
    DatabaseError,
    RecordValidationError,
)
from app.orbit.models import OrbitalRecord, StoredOrbitalRecord
from app.orbit.service import OrbitService
from app.orbit.storage import OrbitStorage, parse_epoch_timestamp, resolve_sqlite_path

client = TestClient(app)

MOCK_ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-11-15T04:56:48.710112",
    "MEAN_MOTION": 15.49388394,
    "ECCENTRICITY": 0.00073773,
    "INCLINATION": 51.6322,
    "RA_OF_ASC_NODE": 272.7605,
    "ARG_OF_PERICENTER": 27.6155,
    "MEAN_ANOMALY": 332.5225,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 59516,
    "BSTAR": 0.00019362061,
    "MEAN_MOTION_DOT": 0.00010382,
    "MEAN_MOTION_DDOT": 0.0,
}

MOCK_CSS_OMM = {
    "OBJECT_NAME": "CSS (TIANHE)",
    "OBJECT_ID": "2021-035A",
    "EPOCH": "2026-11-15T05:10:00.000000",
    "MEAN_MOTION": 15.61234567,
    "ECCENTRICITY": 0.00034567,
    "INCLINATION": 41.4723,
    "RA_OF_ASC_NODE": 120.1234,
    "ARG_OF_PERICENTER": 80.5678,
    "MEAN_ANOMALY": 200.1234,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 48274,
}


@pytest.fixture
def mem_storage():
    """Isolated in-memory SQLite storage fixture."""
    storage = OrbitStorage(db_url=":memory:")
    yield storage
    storage.close()


# ===================== Path & Epoch Parsing Tests =====================

def test_resolve_sqlite_path():
    assert resolve_sqlite_path(":memory:") == ":memory:"
    assert resolve_sqlite_path("sqlite:///:memory:") == ":memory:"
    resolved = resolve_sqlite_path("sqlite:///./satview.db")
    assert resolved.endswith("satview.db")


def test_storage_nested_directory_creation():
    """Verify that nested non-existent directory hierarchies are created automatically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nested_db = Path(tmpdir) / "level1" / "level2" / "level3" / "satview_nested.db"
        assert not nested_db.parent.exists()

        storage = OrbitStorage(db_url=f"sqlite:///{nested_db}")
        try:
            assert nested_db.parent.exists()
            assert nested_db.exists()

            rec = OrbitalRecord.model_validate(MOCK_ISS_OMM)
            assert storage.upsert(rec) is True
            assert storage.count() == 1
            stored = storage.get(25544)
            assert stored is not None
            assert stored.object_name == "ISS (ZARYA)"
        finally:
            storage.close()


def test_parse_epoch_timestamp_formats():
    ts1 = parse_epoch_timestamp("2026-11-15T04:56:48.710112")
    ts2 = parse_epoch_timestamp("2026-11-15T04:56:48.710112Z")
    ts3 = parse_epoch_timestamp("2026-11-15T04:56:48.710112+00:00")
    assert ts1 == pytest.approx(ts2)
    assert ts1 == pytest.approx(ts3)

    ts_no_micro = parse_epoch_timestamp("2026-11-15T04:56:48")
    assert ts_no_micro < ts1

    with pytest.raises(RecordValidationError):
        parse_epoch_timestamp("not-a-valid-timestamp")


# ===================== Storage Engine Unit Tests =====================

def test_storage_init_empty(mem_storage: OrbitStorage):
    assert mem_storage.count() == 0
    assert mem_storage.list_records() == []
    assert mem_storage.get(25544) is None
    assert mem_storage.get_record(25544) is None


def test_storage_upsert_fresh_record(mem_storage: OrbitStorage):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    custom_time = "2026-11-16T12:00:00.000000+00:00"

    inserted = mem_storage.upsert(record, source="celestrak_test", retrieved_at=custom_time)
    assert inserted is True
    assert mem_storage.count() == 1

    stored = mem_storage.get(25544)
    assert stored is not None
    assert stored.norad_cat_id == 25544
    assert stored.object_name == "ISS (ZARYA)"
    assert stored.object_id == "1998-067A"
    assert stored.epoch == "2026-11-15T04:56:48.710112"
    assert stored.source == "celestrak_test"
    assert stored.retrieved_at == custom_time
    assert stored.epoch_timestamp == pytest.approx(parse_epoch_timestamp(record.epoch))

    # Base OrbitalRecord conversion
    base_rec = stored.to_orbital_record()
    assert isinstance(base_rec, OrbitalRecord)
    assert base_rec.norad_cat_id == 25544
    assert base_rec.inclination == 51.6322


def test_storage_upsert_newer_record_overwrites(mem_storage: OrbitStorage):
    """Verify that incoming records with a newer epoch overwrite existing records."""
    record_t0 = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    mem_storage.upsert(record_t0, source="celestrak", retrieved_at="2026-11-15T05:00:00Z")

    # Newer record with epoch 2 hours later and slightly shifted mean motion
    newer_data = {
        **MOCK_ISS_OMM,
        "EPOCH": "2026-11-15T06:56:48.710112",
        "MEAN_MOTION": 15.49500000,
        "ELEMENT_SET_NO": 1000,
    }
    record_t1 = OrbitalRecord.model_validate(newer_data)

    updated = mem_storage.upsert(record_t1, source="celestrak_update", retrieved_at="2026-11-15T07:00:00Z")
    assert updated is True
    assert mem_storage.count() == 1  # Keyed by NORAD ID, no duplicate rows

    stored = mem_storage.get(25544)
    assert stored is not None
    assert stored.epoch == "2026-11-15T06:56:48.710112"
    assert stored.mean_motion == pytest.approx(15.49500000)
    assert stored.element_set_no == 1000
    assert stored.source == "celestrak_update"
    assert stored.retrieved_at == "2026-11-15T07:00:00Z"


def test_storage_upsert_stale_record_rejected(mem_storage: OrbitStorage):
    """Verify Requirement 7: Stale/older incoming records NEVER overwrite newer stored records."""
    # Store initial record at T1
    newer_data = {
        **MOCK_ISS_OMM,
        "EPOCH": "2026-11-15T06:56:48.710112",
        "MEAN_MOTION": 15.49500000,
    }
    record_t1 = OrbitalRecord.model_validate(newer_data)
    mem_storage.upsert(record_t1, source="newer_source", retrieved_at="2026-11-15T07:00:00Z")

    # Now attempt to upsert an older record at T0 (2 hours earlier)
    record_t0 = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    was_modified = mem_storage.upsert(record_t0, source="stale_source", retrieved_at="2026-11-15T08:00:00Z")

    # Must be rejected (not modified)
    assert was_modified is False
    assert mem_storage.count() == 1

    # Stored record must strictly preserve the newer T1 data
    stored = mem_storage.get(25544)
    assert stored is not None
    assert stored.epoch == "2026-11-15T06:56:48.710112"
    assert stored.mean_motion == pytest.approx(15.49500000)
    assert stored.source == "newer_source"
    assert stored.retrieved_at == "2026-11-15T07:00:00Z"


def test_storage_upsert_equal_epoch(mem_storage: OrbitStorage):
    """Equal epoch updates retrieval metadata."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    mem_storage.upsert(record, source="fetch_1", retrieved_at="2026-11-15T05:00:00Z")

    updated = mem_storage.upsert(record, source="fetch_2", retrieved_at="2026-11-15T05:30:00Z")
    assert updated is True
    assert mem_storage.count() == 1

    stored = mem_storage.get(25544)
    assert stored.source == "fetch_2"
    assert stored.retrieved_at == "2026-11-15T05:30:00Z"


def test_storage_batch_upsert_many(mem_storage: OrbitStorage):
    rec_iss = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    rec_css = OrbitalRecord.model_validate(MOCK_CSS_OMM)

    # Batch insert 2 records
    count = mem_storage.upsert_many([rec_iss, rec_css], source="batch_init")
    assert count == 2
    assert mem_storage.count() == 2

    # Batch update: 1 newer ISS, 1 stale CSS
    newer_iss = OrbitalRecord.model_validate({**MOCK_ISS_OMM, "EPOCH": "2026-11-15T08:00:00Z"})
    stale_css = OrbitalRecord.model_validate({**MOCK_CSS_OMM, "EPOCH": "2026-11-15T01:00:00Z"})

    updated_count = mem_storage.upsert_many([newer_iss, stale_css], source="batch_mixed")
    # Only the newer ISS should have updated
    assert updated_count == 1
    assert mem_storage.count() == 2
    assert mem_storage.get(25544).epoch == "2026-11-15T08:00:00Z"
    assert mem_storage.get(48274).epoch == "2026-11-15T05:10:00.000000"


def test_storage_upsert_many_atomic_rollback_on_failure(mem_storage: OrbitStorage):
    """Verify that if one record in upsert_many fails, earlier batch records are rolled back."""
    rec1 = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    rec2 = OrbitalRecord.model_validate(MOCK_CSS_OMM)

    # Construct rec3 with invalid epoch that raises RecordValidationError during batch execution
    rec3 = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    rec3.norad_cat_id = 99999
    rec3.epoch = "corrupted-epoch-timestamp"

    with pytest.raises(RecordValidationError):
        mem_storage.upsert_many([rec1, rec2, rec3])

    # Ensure transaction was fully rolled back: neither rec1 nor rec2 was committed
    assert mem_storage.count() == 0
    assert mem_storage.get(25544) is None
    assert mem_storage.get(48274) is None
    assert mem_storage.get(99999) is None


def test_storage_list_pagination_and_delete(mem_storage: OrbitStorage):
    rec_iss = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    rec_css = OrbitalRecord.model_validate(MOCK_CSS_OMM)
    mem_storage.upsert_many([rec_iss, rec_css])

    # Listing
    items = mem_storage.list_records(limit=1, offset=0)
    assert len(items) == 1
    assert items[0].norad_cat_id == 25544

    items_page2 = mem_storage.list_records(limit=1, offset=1)
    assert len(items_page2) == 1
    assert items_page2[0].norad_cat_id == 48274

    # Delete
    deleted = mem_storage.delete(25544)
    assert deleted is True
    assert mem_storage.count() == 1
    assert mem_storage.get(25544) is None

    # Delete non-existent
    deleted_false = mem_storage.delete(999999)
    assert deleted_false is False


def test_storage_clear(mem_storage: OrbitStorage):
    rec_iss = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    mem_storage.upsert(rec_iss)
    assert mem_storage.count() == 1

    mem_storage.clear()
    assert mem_storage.count() == 0


def test_storage_database_error_on_closed_connection(mem_storage: OrbitStorage):
    """Verify DatabaseError is raised when database operations fail."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    mem_storage.close()

    with pytest.raises(DatabaseError) as exc_info:
        mem_storage.upsert(record)
    assert "connection is closed" in str(exc_info.value).lower()

    with pytest.raises(DatabaseError):
        mem_storage.get(25544)

    with pytest.raises(DatabaseError):
        mem_storage.count()


def test_storage_concurrent_thread_access():
    """Verify thread-safety and lack of race conditions using ThreadPoolExecutor across 50+ operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "concurrent.db"
        storage = OrbitStorage(db_url=f"sqlite:///{db_file}")

        # Seed baseline ISS with intermediate epoch T_mid
        base_iss = OrbitalRecord.model_validate({**MOCK_ISS_OMM, "EPOCH": "2026-11-15T05:00:00Z"})
        storage.upsert(base_iss)

        def worker_op(task_idx: int):
            op_type = task_idx % 5
            if op_type == 0:
                # Upsert newer ISS (T_new > T_mid)
                rec = OrbitalRecord.model_validate({
                    **MOCK_ISS_OMM,
                    "EPOCH": "2026-11-15T08:00:00Z",
                    "MEAN_MOTION": 15.55,
                })
                return ("upsert_newer", storage.upsert(rec))
            elif op_type == 1:
                # Stale ISS attempt (T_old < T_mid) - must be rejected
                rec = OrbitalRecord.model_validate({
                    **MOCK_ISS_OMM,
                    "EPOCH": "2026-11-15T01:00:00Z",
                    "MEAN_MOTION": 15.40,
                })
                return ("upsert_stale", storage.upsert(rec))
            elif op_type == 2:
                # Read ISS
                return ("get_iss", storage.get(25544))
            elif op_type == 3:
                # Insert unique satellite
                unique_cat_id = 70000 + task_idx
                rec = OrbitalRecord.model_validate({
                    **MOCK_CSS_OMM,
                    "NORAD_CAT_ID": unique_cat_id,
                    "EPOCH": "2026-11-15T06:00:00Z",
                })
                return ("upsert_unique", storage.upsert(rec))
            else:
                # List records
                return ("list", storage.list_records(limit=25))

        # Execute 60 concurrent operations across 12 worker threads
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(worker_op, i) for i in range(60)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)

        # Verify no thread encountered database errors or locking collisions
        assert errors == [], f"Concurrent thread access encountered errors: {errors}"

        # Verify final state integrity
        final_iss = storage.get(25544)
        assert final_iss is not None
        assert final_iss.epoch == "2026-11-15T08:00:00Z"
        assert final_iss.mean_motion == pytest.approx(15.55)

        # 60 tasks: op_type == 3 occurs for 12 unique satellites (indices 3, 8, 13, 18, ..., 58) + 1 ISS = 13 total
        assert storage.count() == 13

        storage.close()


# ===================== OrbitService Integration Tests =====================

@pytest.mark.anyio
async def test_service_persists_on_successful_fetch(mem_storage: OrbitStorage):
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_catnr.return_value = [MOCK_ISS_OMM]

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache, storage=mem_storage)

    # 1. Fetch satellite
    rec = await service.get_satellite_by_catnr(25544)
    assert rec is not None
    assert rec.norad_cat_id == 25544

    # 2. Verify stored record exists in persistent storage
    stored = await service.get_stored_satellite(25544)
    assert stored is not None
    assert stored.norad_cat_id == 25544
    assert stored.object_name == "ISS (ZARYA)"
    assert stored.source == "celestrak"
    assert stored.retrieved_at is not None

    # 3. Query stored count
    assert await service.count_stored_satellites() == 1


@pytest.mark.anyio
async def test_service_persists_group_fetch(mem_storage: OrbitStorage):
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_group.return_value = [
        MOCK_ISS_OMM,
        MOCK_CSS_OMM,
        {"OBJECT_NAME": "INVALID_RECORD", "NORAD_CAT_ID": -99},
    ]

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache, storage=mem_storage)

    records = await service.get_satellites_by_group("stations")
    assert len(records) == 2
    assert mem_storage.count() == 2

    # Both valid satellites are stored
    assert mem_storage.get(25544) is not None
    assert mem_storage.get(48274) is not None


@pytest.mark.anyio
async def test_service_fallback_to_storage_on_celestrak_failure(mem_storage: OrbitStorage):
    """Verify fallback_to_storage=True returns stored record when CelesTrak fails."""
    # Pre-populate storage
    rec = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    mem_storage.upsert(rec, source="seed_data")

    # Mock client failure
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_catnr.side_effect = CelesTrakRateLimitError("Rate limit 403")

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache, storage=mem_storage)

    # With fallback_to_storage=True: gracefully returns stored record
    result = await service.get_satellite_by_catnr(25544, use_cache=False, fallback_to_storage=True)
    assert result is not None
    assert result.norad_cat_id == 25544

    # With fallback_to_storage=False (default): strictly raises CelesTrakRateLimitError
    with pytest.raises(CelesTrakRateLimitError):
        await service.get_satellite_by_catnr(25544, use_cache=False, fallback_to_storage=False)


@pytest.mark.anyio
async def test_service_stale_record_from_upstream_does_not_overwrite_storage(mem_storage: OrbitStorage):
    """Upstream returning an older epoch does not corrupt a newer stored record."""
    # Seed storage with newer ISS (epoch T1)
    newer_iss = {**MOCK_ISS_OMM, "EPOCH": "2026-11-15T08:00:00Z", "MEAN_MOTION": 15.5500}
    mem_storage.upsert(OrbitalRecord.model_validate(newer_iss), source="newer_feed")

    # Client returns older ISS (epoch T0)
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_catnr.return_value = [MOCK_ISS_OMM]

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache, storage=mem_storage)

    returned = await service.get_satellite_by_catnr(25544, use_cache=False)
    assert returned.epoch == "2026-11-15T04:56:48.710112"

    # Persistent storage still preserves newer T1 record
    stored = mem_storage.get(25544)
    assert stored.epoch == "2026-11-15T08:00:00Z"
    assert stored.mean_motion == pytest.approx(15.5500)


@pytest.mark.anyio
async def test_service_database_failure_handling(mem_storage: OrbitStorage):
    """Database errors are handled separately from CelesTrak errors."""
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_catnr.return_value = [MOCK_ISS_OMM]

    mem_storage.close()  # Break DB connection

    # With strict_storage=False (default resilience): logs DB error, returns fetched record
    service_resilient = OrbitService(client=mock_client, cache=TTLCache(), storage=mem_storage, strict_storage=False)
    rec = await service_resilient.get_satellite_by_catnr(25544, use_cache=False)
    assert rec is not None
    assert rec.norad_cat_id == 25544

    # With strict_storage=True: raises DatabaseError (NOT CelesTrakError)
    service_strict = OrbitService(client=mock_client, cache=TTLCache(), storage=mem_storage, strict_storage=True)
    with pytest.raises(DatabaseError) as exc_info:
        await service_strict.get_satellite_by_catnr(25544, use_cache=False)
    assert "connection is closed" in str(exc_info.value).lower()


# ===================== FastAPI Router Tests =====================

def test_api_get_stored_satellite_success():
    stored_rec = StoredOrbitalRecord.model_validate({
        **MOCK_ISS_OMM,
        "SOURCE": "celestrak",
        "RETRIEVED_AT": "2026-11-16T12:00:00Z",
        "EPOCH_TIMESTAMP": 1794718608.71,
    })
    with patch("app.orbit.router.orbit_service.get_stored_satellite", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = stored_rec
        resp = client.get("/api/orbit/stored/25544")
        assert resp.status_code == 200
        data = resp.json()
        assert data["NORAD_CAT_ID"] == 25544
        assert data["SOURCE"] == "celestrak"
        assert data["RETRIEVED_AT"] == "2026-11-16T12:00:00Z"
        assert data["EPOCH_TIMESTAMP"] == pytest.approx(1794718608.71)


def test_api_get_stored_satellite_not_found():
    with patch("app.orbit.router.orbit_service.get_stored_satellite", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None
        resp = client.get("/api/orbit/stored/999999")
        assert resp.status_code == 404
        assert "no stored orbital record" in resp.json()["detail"].lower()


def test_api_get_stored_satellite_database_error_500():
    with patch("app.orbit.router.orbit_service.get_stored_satellite", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = DatabaseError("SQLite I/O failure")
        resp = client.get("/api/orbit/stored/25544")
        assert resp.status_code == 500
        assert "SQLite I/O failure" in resp.json()["detail"]


def test_api_list_stored_satellites():
    stored_rec = StoredOrbitalRecord.model_validate({
        **MOCK_ISS_OMM,
        "SOURCE": "celestrak",
        "RETRIEVED_AT": "2026-11-16T12:00:00Z",
        "EPOCH_TIMESTAMP": 1794718608.71,
    })
    with patch("app.orbit.router.orbit_service.list_stored_satellites", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = [stored_rec]
        resp = client.get("/api/orbit/stored?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["NORAD_CAT_ID"] == 25544
