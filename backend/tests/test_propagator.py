from datetime import datetime, timezone
import math
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orbit.exceptions import PropagationError
from app.orbit.models import OrbitalRecord, PropagationResult
from app.orbit.propagator import OrbitPropagator

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

# Synthetic Geostationary Earth Orbit (GEO) satellite elements
MOCK_GEO_OMM = {
    "OBJECT_NAME": "COMM-SAT GEO",
    "OBJECT_ID": "2020-001A",
    "EPOCH": "2026-11-15T00:00:00.000000",
    "MEAN_MOTION": 1.0027379,  # ~1 rev per sidereal day
    "ECCENTRICITY": 0.0001,
    "INCLINATION": 0.05,
    "RA_OF_ASC_NODE": 100.0,
    "ARG_OF_PERICENTER": 20.0,
    "MEAN_ANOMALY": 50.0,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 45000,
    "ELEMENT_SET_NO": 100,
    "REV_AT_EPOCH": 2000,
    "BSTAR": 0.0,
    "MEAN_MOTION_DOT": 0.0,
    "MEAN_MOTION_DDOT": 0.0,
}


@pytest.fixture
def propagator():
    return OrbitPropagator()


# ===================== SGP4 Mathematical & Deterministic Tests =====================

def test_propagate_iss_known_reference_state(propagator):
    """Verify ISS propagation matches deterministic astrodynamics reference numbers."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    target_time = "2026-11-15T05:00:00Z"

    result = propagator.propagate(record, target_time)

    assert isinstance(result, PropagationResult)
    assert result.norad_cat_id == 25544
    assert result.object_name == "ISS (ZARYA)"
    assert result.coordinate_frame == "GCRS"
    assert result.timestamp == "2026-11-15T05:00:00+00:00"

    # Known reference coordinates for this epoch and target
    assert result.position_km.x == pytest.approx(1184.616, abs=0.1)
    assert result.position_km.y == pytest.approx(-6589.511, abs=0.1)
    assert result.position_km.z == pytest.approx(1138.189, abs=0.1)

    # Physical verification: LEO orbit radius ~6,791 km (altitude ~413 km above 6378 km Earth radius)
    r = math.sqrt(
        result.position_km.x**2 + result.position_km.y**2 + result.position_km.z**2
    )
    assert r == pytest.approx(6791.2, abs=1.0)

    # Known reference velocity vectors
    assert result.velocity_km_per_s.x == pytest.approx(4.587, abs=0.05)
    assert result.velocity_km_per_s.y == pytest.approx(1.846, abs=0.05)
    assert result.velocity_km_per_s.z == pytest.approx(5.860, abs=0.05)

    # Physical verification: LEO circular orbital speed ~7.67 km/s
    v = math.sqrt(
        result.velocity_km_per_s.x**2
        + result.velocity_km_per_s.y**2
        + result.velocity_km_per_s.z**2
    )
    assert v == pytest.approx(7.668, abs=0.05)


def test_propagate_geo_satellite_physics(propagator):
    """Verify geostationary satellite produces GEO altitude (~35,786 km) and speed (~3.07 km/s)."""
    record = OrbitalRecord.model_validate(MOCK_GEO_OMM)
    target_time = datetime(2026, 11, 15, 6, 0, 0, tzinfo=timezone.utc)

    result = propagator.propagate(record, target_time)

    # GEO orbital radius: ~42,164 km
    r = math.sqrt(
        result.position_km.x**2 + result.position_km.y**2 + result.position_km.z**2
    )
    assert r == pytest.approx(42164.0, rel=0.02)

    # GEO orbital velocity: ~3.075 km/s
    v = math.sqrt(
        result.velocity_km_per_s.x**2
        + result.velocity_km_per_s.y**2
        + result.velocity_km_per_s.z**2
    )
    assert v == pytest.approx(3.075, rel=0.02)


# ===================== Timestamp Handling Tests =====================

def test_propagate_timestamp_iso_z_suffix(propagator):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    result = propagator.propagate(record, "2026-11-15T05:00:00Z")
    assert result.timestamp == "2026-11-15T05:00:00+00:00"


def test_propagate_timestamp_timezone_offset(propagator):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    # 10:30 IST is 05:00 UTC
    result = propagator.propagate(record, "2026-11-15T10:30:00+05:30")
    assert result.timestamp == "2026-11-15T05:00:00+00:00"
    assert result.position_km.x == pytest.approx(1184.616, abs=0.1)


def test_propagate_timestamp_naive_datetime(propagator):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    naive_dt = datetime(2026, 11, 15, 5, 0, 0)
    result = propagator.propagate(record, naive_dt)
    assert result.timestamp == "2026-11-15T05:00:00+00:00"


def test_propagate_invalid_timestamp_string(propagator):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    with pytest.raises(PropagationError) as exc_info:
        propagator.propagate(record, "not-a-timestamp")
    assert "Invalid ISO 8601 timestamp" in str(exc_info.value)


# ===================== Error Handling & Edge Cases =====================

def test_propagate_decayed_satellite_raises_error(propagator):
    """Verify SGP4 numerical failure/decay on extreme future date raises a PropagationError."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    # Propagate 20 years into the future where drag/perturbations cause SGP4 calculation failure
    decay_time = datetime(2046, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(PropagationError) as exc_info:
        propagator.propagate(record, decay_time)
    assert "sgp4 propagation error" in str(exc_info.value).lower()



def test_propagate_epoch_normalization_missing_microseconds(propagator):
    """Verify SGP4 initialization succeeds even when EPOCH string lacks microseconds."""
    raw = {**MOCK_ISS_OMM, "EPOCH": "2026-11-15T04:56:48"}
    record = OrbitalRecord.model_validate(raw)
    result = propagator.propagate(record, "2026-11-15T05:00:00Z")
    assert result.position_km.x is not None


# ===================== API Route Tests =====================

def test_api_propagate_endpoint_success():
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = OrbitalRecord.model_validate(MOCK_ISS_OMM)
        resp = client.get(
            "/api/orbit/satellite/25544/propagate?time=2026-11-15T05:00:00Z"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["norad_cat_id"] == 25544
        assert data["coordinate_frame"] == "GCRS"
        assert "position_km" in data
        assert "velocity_km_per_s" in data
        assert data["position_km"]["x"] == pytest.approx(1184.616, abs=0.1)


def test_api_propagate_endpoint_satellite_not_found():
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = None
        resp = client.get("/api/orbit/satellite/999999/propagate")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


def test_api_propagate_endpoint_invalid_time():
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = OrbitalRecord.model_validate(MOCK_ISS_OMM)
        resp = client.get(
            "/api/orbit/satellite/25544/propagate?time=invalid-date"
        )
        assert resp.status_code == 400
        assert "invalid iso 8601" in resp.json()["detail"].lower()


def test_api_propagate_endpoint_invalid_catnr_422():
    resp = client.get("/api/orbit/satellite/-10/propagate")
    assert resp.status_code == 422
