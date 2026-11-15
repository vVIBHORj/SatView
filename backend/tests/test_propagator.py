from datetime import datetime, timezone
import math
from unittest.mock import AsyncMock, patch
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sgp4.api import Satrec, WGS72
from skyfield.api import EarthSatellite
from skyfield.sgp4lib import TEME

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

# Official AIAA 2006 / Vallado SGP4 verification case (Vanguard 1, NORAD 5)
# 1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
# 2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667
VALLADO_SAT5_OMM = {
    "OBJECT_NAME": "VANGUARD 1",
    "OBJECT_ID": "1958-002B",
    "EPOCH": "2000-06-27T18:50:19.733571",
    "MEAN_MOTION": 10.82419157,
    "ECCENTRICITY": 0.1859667,
    "INCLINATION": 34.2682,
    "RA_OF_ASC_NODE": 348.7242,
    "ARG_OF_PERICENTER": 331.7664,
    "MEAN_ANOMALY": 19.3264,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 5,
    "ELEMENT_SET_NO": 475,
    "REV_AT_EPOCH": 41366,
    "BSTAR": 0.000028098,
    "MEAN_MOTION_DOT": 0.00000023,
    "MEAN_MOTION_DDOT": 0.0,
}

# Official AIAA 2006 / Vallado SGP4 verification case (GEO satellite 28626)
# 1 28626U 05008A   06176.46683397 -.00000205  00000-0  10000-3 0  2190
# 2 28626   0.0019 286.9433 0000335  13.7918  55.6504  1.00270176  4891
VALLADO_GEO_28626_OMM = {
    "OBJECT_NAME": "GEO-28626",
    "OBJECT_ID": "2005-008A",
    "EPOCH": "2006-06-25T11:12:14.455012",
    "MEAN_MOTION": 1.00270176,
    "ECCENTRICITY": 0.0000335,
    "INCLINATION": 0.0019,
    "RA_OF_ASC_NODE": 286.9433,
    "ARG_OF_PERICENTER": 13.7918,
    "MEAN_ANOMALY": 55.6504,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 28626,
    "ELEMENT_SET_NO": 219,
    "REV_AT_EPOCH": 489,
    "BSTAR": 0.0001,
    "MEAN_MOTION_DOT": -0.00000205,
    "MEAN_MOTION_DDOT": 0.0,
}


@pytest.fixture
def propagator():
    return OrbitPropagator()


# ===================== SGP4 Mathematical & Independent Reference Tests =====================

def test_wgs72_gravity_constant_model(propagator):
    """Verify SGP4 initialization specifically uses the WGS-72 Earth gravity model."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    sat = EarthSatellite.from_omm(propagator.ts, record.to_omm_dict())

    # WGS-72 standard constants in SGP4
    assert sat.model.radiusearthkm == pytest.approx(6378.135)
    assert sat.model.mu == pytest.approx(398600.8)
    assert sat.model.j2 == pytest.approx(0.001082616)


def test_independent_vallado_benchmark_sat5(propagator):
    """Validate SGP4 against independent Vallado 2006 AIAA benchmark dataset (tcppver.out)."""
    record = OrbitalRecord.model_validate(VALLADO_SAT5_OMM)

    # Vallado published reference coordinates in tcppver.out for Sat 5 at t = +360.0 min
    ref_teme_pos_360 = np.array([-7154.03120202, -3783.17682504, -3536.19412294])
    ref_teme_vel_360 = np.array([4.741887409, -4.151817765, -2.093935425])

    target_iso = "2000-06-28T00:50:19.733571Z"  # epoch + 360 min
    result = propagator.propagate(record, target_iso)

    # Rotate published reference TEME vector into GCRS at date t
    t = propagator.ts.utc(2000, 6, 28, 0, 50, 19.733571)
    R = TEME.rotation_at(t)
    expected_gcrs_pos = R.T @ ref_teme_pos_360
    expected_gcrs_vel = R.T @ ref_teme_vel_360

    calc_pos = np.array([result.position_km.x, result.position_km.y, result.position_km.z])
    calc_vel = np.array([result.velocity_km_per_s.x, result.velocity_km_per_s.y, result.velocity_km_per_s.z])

    # Position error must be < 1 mm (1e-6 km)
    assert np.linalg.norm(calc_pos - expected_gcrs_pos) < 1e-6
    # Velocity error must be < 1e-8 km/s
    assert np.linalg.norm(calc_vel - expected_gcrs_vel) < 1e-8


def test_independent_vallado_benchmark_geo_28626(propagator):
    """Validate GEO satellite SGP4/SDP4 propagation against Vallado 2006 benchmark."""
    record = OrbitalRecord.model_validate(VALLADO_GEO_28626_OMM)

    # Vallado published reference coordinates in tcppver.out for GEO Sat 28626 at t = 0 min
    ref_teme_pos_0 = np.array([42080.71852213, -2646.86387436, 0.81851294])
    ref_teme_vel_0 = np.array([0.193105177, 3.068688251, 0.000438449])

    epoch_iso = "2006-06-25T11:12:14.455012Z"
    result = propagator.propagate(record, epoch_iso)

    t = propagator.ts.utc(2006, 6, 25, 11, 12, 14.455012)
    R = TEME.rotation_at(t)
    expected_gcrs_pos = R.T @ ref_teme_pos_0
    expected_gcrs_vel = R.T @ ref_teme_vel_0

    calc_pos = np.array([result.position_km.x, result.position_km.y, result.position_km.z])
    calc_vel = np.array([result.velocity_km_per_s.x, result.velocity_km_per_s.y, result.velocity_km_per_s.z])

    # Position error must be < 1 mm
    assert np.linalg.norm(calc_pos - expected_gcrs_pos) < 1e-6
    # Velocity error must be < 1e-8 km/s
    assert np.linalg.norm(calc_vel - expected_gcrs_vel) < 1e-8


def test_coordinate_frame_distinction_teme_vs_gcrs(propagator):
    """Verify that returned coordinates are genuinely GCRS, distinctly rotated from TEME."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    target_time = "2026-11-15T05:00:00Z"

    result = propagator.propagate(record, target_time)

    # Calculate raw SGP4 TEME coordinates
    t = propagator.ts.utc(2026, 11, 15, 5, 0, 0)
    sat = EarthSatellite.from_omm(propagator.ts, record.to_omm_dict())
    r_teme, v_teme, err = sat._position_and_velocity_TEME_km(t)

    calc_gcrs_pos = np.array([result.position_km.x, result.position_km.y, result.position_km.z])
    calc_gcrs_vel = np.array([result.velocity_km_per_s.x, result.velocity_km_per_s.y, result.velocity_km_per_s.z])

    # Physical distinction: precession & nutation between J2000 (GCRS) and 2026 (TEME)
    # produce a physical vector difference of ~37.4 km for the ISS
    frame_diff_km = np.linalg.norm(calc_gcrs_pos - r_teme)
    assert frame_diff_km > 30.0  # Confirms result is NOT mistakenly returned in raw TEME
    assert frame_diff_km == pytest.approx(37.4, abs=1.0)

    # Exact rotation verification: GCRS must match R^T @ TEME to nanometre precision
    R = TEME.rotation_at(t)
    expected_gcrs_pos = R.T @ r_teme
    expected_gcrs_vel = R.T @ v_teme

    assert np.linalg.norm(calc_gcrs_pos - expected_gcrs_pos) < 1e-9
    assert np.linalg.norm(calc_gcrs_vel - expected_gcrs_vel) < 1e-12


# ===================== Timestamp Handling Tests =====================

def test_propagate_timestamp_iso_z_suffix(propagator):
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    result = propagator.propagate(record, "2026-11-15T05:00:00Z")
    assert result.timestamp == "2026-11-15T05:00:00+00:00"


def test_propagate_timestamp_fractional_seconds(propagator):
    """Verify timestamps with fractional seconds and explicit offsets are handled cleanly."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    result = propagator.propagate(record, "2026-11-15T05:00:00.123456Z")
    assert result.timestamp == "2026-11-15T05:00:00.123456+00:00"

    # Non-UTC offset with fractional seconds (10:30:00.5 IST -> 05:00:00.5 UTC)
    result_ist = propagator.propagate(record, "2026-11-15T10:30:00.500000+05:30")
    assert result_ist.timestamp == "2026-11-15T05:00:00.500000+00:00"


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
