import time
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orbit.cache import TTLCache
from app.orbit.celestrak import CelesTrakClient
from app.orbit.exceptions import (
    CelesTrakError,
    CelesTrakTimeoutError,
    RecordValidationError,
)
from app.orbit.models import OrbitalRecord
from app.orbit.service import OrbitService

client = TestClient(app)

MOCK_ISS_RAW = {
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

MOCK_CSS_RAW = {
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


# ===================== Models Tests =====================

def test_orbital_record_valid_parsing():
    rec = OrbitalRecord.model_validate(MOCK_ISS_RAW)
    assert rec.object_name == "ISS (ZARYA)"
    assert rec.norad_cat_id == 25544
    assert rec.inclination == 51.6322
    assert rec.bstar == pytest.approx(0.00019362061)


def test_orbital_record_invalid_cat_id():
    bad_data = {**MOCK_ISS_RAW, "NORAD_CAT_ID": -5}
    with pytest.raises(Exception):
        OrbitalRecord.model_validate(bad_data)


def test_orbital_record_invalid_eccentricity():
    bad_data = {**MOCK_ISS_RAW, "ECCENTRICITY": -0.1}
    with pytest.raises(Exception):
        OrbitalRecord.model_validate(bad_data)


def test_orbital_record_invalid_mean_motion():
    bad_data = {**MOCK_ISS_RAW, "MEAN_MOTION": 0.0}
    with pytest.raises(Exception):
        OrbitalRecord.model_validate(bad_data)


# ===================== Cache Tests =====================

def test_ttl_cache_basic_operations():
    cache = TTLCache(default_ttl_seconds=60)
    cache.set("key1", "val1")
    assert cache.contains("key1") is True
    assert cache.get("key1") == "val1"
    assert len(cache) == 1

    cache.delete("key1")
    assert cache.get("key1") is None
    assert len(cache) == 0


def test_ttl_cache_expiration():
    cache = TTLCache(default_ttl_seconds=1)
    cache.set("short_lived", "data", ttl=0)  # expires immediately
    time.sleep(0.05)
    assert cache.get("short_lived") is None
    assert cache.contains("short_lived") is False


# ===================== Client Adapter Tests =====================

@pytest.mark.anyio
async def test_client_fetch_by_catnr_success():
    mock_resp = httpx.Response(
        status_code=200,
        content=b'[{"OBJECT_NAME":"ISS (ZARYA)","OBJECT_ID":"1998-067A","EPOCH":"2026-11-15T04:56:48.710112","MEAN_MOTION":15.49388394,"ECCENTRICITY":0.00073773,"INCLINATION":51.6322,"RA_OF_ASC_NODE":272.7605,"ARG_OF_PERICENTER":27.6155,"MEAN_ANOMALY":332.5225,"EPHEMERIS_TYPE":0,"CLASSIFICATION_TYPE":"U","NORAD_CAT_ID":25544}]',
        request=httpx.Request("GET", "https://celestrak.org"),
    )
    async_mock = AsyncMock(spec=httpx.AsyncClient)
    async_mock.get.return_value = mock_resp

    client = CelesTrakClient(client=async_mock)
    records = await client.fetch_gp_by_catnr(25544)
    assert len(records) == 1
    assert records[0]["NORAD_CAT_ID"] == 25544


@pytest.mark.anyio
async def test_client_fetch_404_not_found():
    mock_resp = httpx.Response(
        status_code=404,
        text="No GP data found",
        request=httpx.Request("GET", "https://celestrak.org"),
    )
    async_mock = AsyncMock(spec=httpx.AsyncClient)
    async_mock.get.return_value = mock_resp

    client = CelesTrakClient(client=async_mock)
    records = await client.fetch_gp_by_catnr(999999)
    assert records == []


@pytest.mark.anyio
async def test_client_fetch_text_error_response():
    mock_resp = httpx.Response(
        status_code=200,
        text='Invalid query: "GROUP=bad_group&FORMAT=JSON"',
        request=httpx.Request("GET", "https://celestrak.org"),
    )
    async_mock = AsyncMock(spec=httpx.AsyncClient)
    async_mock.get.return_value = mock_resp

    client = CelesTrakClient(client=async_mock)
    with pytest.raises(CelesTrakError) as exc_info:
        await client.fetch_gp_by_group("bad_group")
    assert "CelesTrak rejected query" in str(exc_info.value)


@pytest.mark.anyio
async def test_client_fetch_timeout():
    async_mock = AsyncMock(spec=httpx.AsyncClient)
    async_mock.get.side_effect = httpx.TimeoutException("Timed out")

    client = CelesTrakClient(client=async_mock)
    with pytest.raises(CelesTrakTimeoutError):
        await client.fetch_gp_by_catnr(25544)


@pytest.mark.anyio
async def test_client_fetch_network_error():
    async_mock = AsyncMock(spec=httpx.AsyncClient)
    async_mock.get.side_effect = httpx.RequestError("Connection refused")

    client = CelesTrakClient(client=async_mock)
    with pytest.raises(CelesTrakError):
        await client.fetch_gp_by_catnr(25544)


# ===================== Service Tests =====================

@pytest.mark.anyio
async def test_service_caching_behavior():
    mock_client = AsyncMock(spec=CelesTrakClient)
    mock_client.fetch_gp_by_catnr.return_value = [MOCK_ISS_RAW]

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache)

    # First call: hits client
    rec1 = await service.get_satellite_by_catnr(25544, use_cache=True)
    assert rec1 is not None
    assert rec1.norad_cat_id == 25544
    assert mock_client.fetch_gp_by_catnr.call_count == 1

    # Second call: served from cache without invoking client
    rec2 = await service.get_satellite_by_catnr(25544, use_cache=True)
    assert rec2 is not None
    assert mock_client.fetch_gp_by_catnr.call_count == 1

    # Third call with use_cache=False: bypasses cache and calls client
    rec3 = await service.get_satellite_by_catnr(25544, use_cache=False)
    assert rec3 is not None
    assert mock_client.fetch_gp_by_catnr.call_count == 2


@pytest.mark.anyio
async def test_service_group_lookup_and_individual_cache_priming():
    mock_client = AsyncMock(spec=CelesTrakClient)
    # Include one valid CSS, one valid ISS, and one invalid record
    invalid_record = {"OBJECT_NAME": "BROKEN", "NORAD_CAT_ID": -1}
    mock_client.fetch_gp_by_group.return_value = [MOCK_ISS_RAW, MOCK_CSS_RAW, invalid_record]

    cache = TTLCache(default_ttl_seconds=3600)
    service = OrbitService(client=mock_client, cache=cache)

    group_records = await service.get_satellites_by_group("stations")
    # Should safely parse valid records and skip broken one
    assert len(group_records) == 2
    cat_ids = [r.norad_cat_id for r in group_records]
    assert 25544 in cat_ids
    assert 48274 in cat_ids

    # Verify individual cache was primed: lookup for ISS requires 0 client calls
    cached_iss = await service.get_satellite_by_catnr(25544, use_cache=True)
    assert cached_iss is not None
    assert cached_iss.object_name == "ISS (ZARYA)"
    assert mock_client.fetch_gp_by_catnr.call_count == 0


# ===================== FastAPI Router Tests =====================

def test_api_get_satellite_success():
    with patch("app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = OrbitalRecord.model_validate(MOCK_ISS_RAW)
        resp = client.get("/api/orbit/satellite/25544")
        assert resp.status_code == 200
        data = resp.json()
        assert data["NORAD_CAT_ID"] == 25544
        assert data["OBJECT_NAME"] == "ISS (ZARYA)"


def test_api_get_satellite_not_found():
    with patch("app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None
        resp = client.get("/api/orbit/satellite/999999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


def test_api_get_group_success():
    with patch("app.orbit.router.orbit_service.get_satellites_by_group", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [
            OrbitalRecord.model_validate(MOCK_ISS_RAW),
            OrbitalRecord.model_validate(MOCK_CSS_RAW),
        ]
        resp = client.get("/api/orbit/group/stations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["NORAD_CAT_ID"] == 25544


def test_api_timeout_handling():
    with patch("app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = CelesTrakTimeoutError("Request timed out")
        resp = client.get("/api/orbit/satellite/25544")
        assert resp.status_code == 504
