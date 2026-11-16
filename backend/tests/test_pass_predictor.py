from datetime import datetime, timezone
import math
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from skyfield.api import EarthSatellite, load, wgs84

from app.main import app
from app.orbit.exceptions import PassPredictionError
from app.orbit.models import (
    ObserverLocation,
    OrbitalRecord,
    PassPredictionResult,
    SatellitePass,
)
from app.orbit.pass_predictor import PassPredictionService

client = TestClient(app)

MOCK_ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-11-15T00:00:00.000000",
    "MEAN_MOTION": 15.50000000,
    "ECCENTRICITY": 0.0005000,
    "INCLINATION": 51.6400,
    "RA_OF_ASC_NODE": 208.0000,
    "ARG_OF_PERICENTER": 130.0000,
    "MEAN_ANOMALY": 230.0000,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 59516,
    "BSTAR": 0.00010270,
    "MEAN_MOTION_DOT": 0.00016717,
    "MEAN_MOTION_DDOT": 0.0,
}

# Vanguard 1 (eccentric orbit with e = 0.1859667)
MOCK_VANGUARD_OMM = {
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

# Geostationary satellite (INMARSAT 4-F1, NORAD 28626)
MOCK_GEO_OMM = {
    "OBJECT_NAME": "INMARSAT 4-F1",
    "OBJECT_ID": "2005-007A",
    "EPOCH": "2026-11-15T00:00:00.000000",
    "MEAN_MOTION": 1.00270000,
    "ECCENTRICITY": 0.0003000,
    "INCLINATION": 0.0500,
    "RA_OF_ASC_NODE": 64.0000,
    "ARG_OF_PERICENTER": 270.0000,
    "MEAN_ANOMALY": 90.0000,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 28626,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 7500,
    "BSTAR": 0.0,
    "MEAN_MOTION_DOT": 0.00000050,
    "MEAN_MOTION_DDOT": 0.0,
}


def test_predict_multiple_passes_iss():
    """Verify PassPredictionService detects multiple passes for ISS over 24 hours."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    start_time = "2026-11-15T00:00:00Z"
    end_time = "2026-11-16T00:00:00Z"
    result = service.predict_passes(
        record, observer, start_time, end_time, min_elevation_deg=10.0
    )

    assert isinstance(result, PassPredictionResult)
    assert result.pass_count == 4
    assert len(result.passes) == 4

    # All passes are fully enclosed within the 24h window
    for p in result.passes:
        assert p.is_complete is True
        assert p.is_continuous is False
        assert 0.0 <= p.max_elevation.azimuth_deg < 360.0
        assert p.duration_s > 100.0


def test_root_finding_elevation_precision():

    """Verify that AOS and LOS crossing times satisfy elevation == min_elevation to high precision."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    min_elevation = 15.0
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T00:00:00Z",
        "2026-11-16T00:00:00Z",
        min_elevation_deg=min_elevation,
    )

    ts = load.timescale(builtin=True)
    sat = EarthSatellite.from_omm(ts, record.to_omm_dict())
    ground = wgs84.latlon(observer.latitude, observer.longitude, observer.elevation_m)
    diff = sat - ground

    for p in result.passes:
        # AOS crossing precision
        t_aos = ts.from_datetime(datetime.fromisoformat(p.aos.time))
        alt_aos = diff.at(t_aos).altaz(temperature_C=None)[0].degrees
        assert math.isclose(alt_aos, min_elevation, abs_tol=1e-3)
        assert math.isclose(p.aos.elevation_deg, min_elevation, abs_tol=1e-3)

        # LOS crossing precision
        t_los = ts.from_datetime(datetime.fromisoformat(p.los.time))
        alt_los = diff.at(t_los).altaz(temperature_C=None)[0].degrees
        assert math.isclose(alt_los, min_elevation, abs_tol=1e-3)
        assert math.isclose(p.los.elevation_deg, min_elevation, abs_tol=1e-3)

        # Culmination must be a true local maximum
        t_culm = ts.from_datetime(datetime.fromisoformat(p.max_elevation.time))
        alt_culm = diff.at(t_culm).altaz(temperature_C=None)[0].degrees
        t_before = ts.utc(
            t_culm.utc_datetime().year,
            t_culm.utc_datetime().month,
            t_culm.utc_datetime().day,
            t_culm.utc_datetime().hour,
            t_culm.utc_datetime().minute,
            t_culm.utc_datetime().second - 2,
        )
        t_after = ts.utc(
            t_culm.utc_datetime().year,
            t_culm.utc_datetime().month,
            t_culm.utc_datetime().day,
            t_culm.utc_datetime().hour,
            t_culm.utc_datetime().minute,
            t_culm.utc_datetime().second + 2,
        )
        assert alt_culm >= diff.at(t_before).altaz(temperature_C=None)[0].degrees
        assert alt_culm >= diff.at(t_after).altaz(temperature_C=None)[0].degrees


def test_distinguish_max_elevation_time_from_min_range_time_eccentric_orbit():
    """Verify that maximum elevation time and minimum slant-range time are distinguished."""
    record = OrbitalRecord.model_validate(MOCK_VANGUARD_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    start_time = "2000-06-27T18:00:00Z"
    end_time = "2000-06-28T18:00:00Z"
    result = service.predict_passes(
        record, observer, start_time, end_time, min_elevation_deg=10.0
    )

    assert result.pass_count > 0

    # In eccentric orbits, max elevation time and min slant range time differ noticeably
    time_diffs = []
    for p in result.passes:
        dt_culm = datetime.fromisoformat(p.max_elevation.time)
        dt_tca = datetime.fromisoformat(p.closest_approach.time)
        diff_seconds = abs((dt_tca - dt_culm).total_seconds())
        time_diffs.append(diff_seconds)

    # At least one pass in an eccentric orbit exhibits > 30 seconds difference
    assert max(time_diffs) > 30.0, f"Expected non-zero difference between TCA and Culmination, got {time_diffs}"


def test_independent_validation_against_skyfield_find_events():
    """Verify PassPredictionService results match Skyfield's native find_events within tolerance."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    start_time = "2026-11-15T00:00:00Z"
    end_time = "2026-11-16T00:00:00Z"
    min_el = 10.0

    result = service.predict_passes(
        record, observer, start_time, end_time, min_elevation_deg=min_el
    )

    # Independent Skyfield run
    ts = load.timescale(builtin=True)
    sat = EarthSatellite.from_omm(ts, record.to_omm_dict())
    ground = wgs84.latlon(observer.latitude, observer.longitude, observer.elevation_m)
    t0 = ts.from_datetime(datetime.fromisoformat(start_time.replace("Z", "+00:00")))
    t1 = ts.from_datetime(datetime.fromisoformat(end_time.replace("Z", "+00:00")))

    times, events = sat.find_events(ground, t0, t1, altitude_degrees=min_el)

    # Skyfield events: 0=rise, 1=culminate, 2=set
    rises = [times[i].utc_datetime() for i, ev in enumerate(events) if ev == 0]
    culms = [times[i].utc_datetime() for i, ev in enumerate(events) if ev == 1]
    sets = [times[i].utc_datetime() for i, ev in enumerate(events) if ev == 2]

    assert len(result.passes) == len(rises) == len(culms) == len(sets)

    for i, p in enumerate(result.passes):
        dt_aos = datetime.fromisoformat(p.aos.time)
        dt_culm = datetime.fromisoformat(p.max_elevation.time)
        dt_los = datetime.fromisoformat(p.los.time)

        # Tolerance: Skyfield find_events uses coarse step and cheat flags, so within 2.0 seconds
        assert abs((dt_aos - rises[i]).total_seconds()) < 2.0
        assert abs((dt_culm - culms[i]).total_seconds()) < 2.0
        assert abs((dt_los - sets[i]).total_seconds()) < 2.0


def test_edge_case_no_passes():
    """Verify window with no passes returns pass_count == 0 and empty list."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Threshold 80 deg (ISS max elevation on this day is 76.17 deg)
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T00:00:00Z",
        "2026-11-16T00:00:00Z",
        min_elevation_deg=80.0,
    )

    assert result.pass_count == 0
    assert result.passes == []


def test_edge_case_grazing_pass():
    """Verify a grazing pass (where satellite barely crests threshold) is captured accurately."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Pass 1 peaks at ~44.37 deg. Setting threshold to 44.30 creates a grazing pass lasting ~15-20s
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T06:15:00Z",
        "2026-11-15T06:25:00Z",
        min_elevation_deg=44.30,
    )

    assert result.pass_count == 1
    p = result.passes[0]
    assert p.is_complete is True
    assert p.max_elevation.elevation_deg >= 44.30
    assert p.duration_s < 30.0


def test_edge_case_window_start_truncation():
    """Verify pass active at start of observation window is marked with is_complete=False."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Pass 1 starts at 06:17:02. Start observation window at 06:19:00 (mid-pass)
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T06:19:00Z",
        "2026-11-15T06:30:00Z",
        min_elevation_deg=10.0,
    )

    assert result.pass_count == 1
    p = result.passes[0]
    assert p.is_complete is False
    assert p.aos.time == "2026-11-15T06:19:00+00:00"


def test_edge_case_window_end_truncation():
    """Verify pass active at end of observation window is marked with is_complete=False."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Pass 1 ends at 06:23:26. End observation window at 06:20:00 (mid-pass)
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T06:10:00Z",
        "2026-11-15T06:20:00Z",
        min_elevation_deg=10.0,
    )

    assert result.pass_count == 1
    p = result.passes[0]
    assert p.is_complete is False
    assert p.los.time == "2026-11-15T06:20:00+00:00"


def test_edge_case_geostationary_continuous_visibility():
    """Verify GEO satellite overhead is recognized as a continuous visibility pass."""
    record = OrbitalRecord.model_validate(MOCK_GEO_OMM)
    # Observer in New Delhi sees INMARSAT 4-F1 continuously at ~11.29 deg elevation
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T00:00:00Z",
        "2026-11-15T12:00:00Z",
        min_elevation_deg=10.0,
    )

    assert result.pass_count == 1
    p = result.passes[0]
    assert p.is_continuous is True
    assert p.is_complete is False
    assert p.duration_s == 12 * 3600.0


def test_edge_case_geostationary_below_horizon():
    """Verify GEO satellite permanently below horizon returns 0 passes."""
    record = OrbitalRecord.model_validate(MOCK_GEO_OMM)
    # Observer on equator in Pacific (-180.0 lon) where this GEO sat is at ~ -42.7 deg elevation
    observer = ObserverLocation(latitude=0.0, longitude=-180.0, elevation_m=0.0)
    service = PassPredictionService()

    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T00:00:00Z",
        "2026-11-15T12:00:00Z",
        min_elevation_deg=10.0,
    )

    assert result.pass_count == 0
    assert result.passes == []



def test_pass_prediction_validation_errors():
    """Verify PassPredictionError is raised for invalid time range and thresholds."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090)
    service = PassPredictionService()

    # End before start
    with pytest.raises(PassPredictionError, match="strictly after start_time"):
        service.predict_passes(
            record,
            observer,
            "2026-11-15T12:00:00Z",
            "2026-11-15T10:00:00Z",
        )

    # Window > 14 days
    with pytest.raises(PassPredictionError, match="exceeds maximum allowable limit"):
        service.predict_passes(
            record,
            observer,
            "2026-11-15T00:00:00Z",
            "2026-12-05T00:00:00Z",
        )

    # Invalid elevation threshold
    with pytest.raises(PassPredictionError, match="min_elevation_deg must be between"):
        service.predict_passes(
            record,
            observer,
            "2026-11-15T00:00:00Z",
            "2026-11-15T12:00:00Z",
            min_elevation_deg=-5.0,
        )


# --- API Endpoint Tests ---


def test_api_passes_endpoint_success():
    """Verify GET /api/orbit/satellite/{catnr}/passes returns 200 and PassPredictionResult."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/passes",
            params={
                "lat": 28.6139,
                "lon": 77.2090,
                "alt_m": 216.0,
                "start_time": "2026-11-15T00:00:00Z",
                "end_time": "2026-11-16T00:00:00Z",
                "min_elevation": 10.0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["norad_cat_id"] == 25544
        assert data["object_name"] == "ISS (ZARYA)"
        assert data["pass_count"] == 4
        assert len(data["passes"]) == 4

        p1 = data["passes"][0]
        assert "aos" in p1
        assert "los" in p1
        assert "max_elevation" in p1
        assert "closest_approach" in p1
        assert p1["duration_s"] > 0


def test_api_passes_endpoint_default_days():
    """Verify GET /api/orbit/satellite/{catnr}/passes computes end_time from days when omitted."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/passes",
            params={
                "lat": 28.6139,
                "lon": 77.2090,
                "start_time": "2026-11-15T00:00:00Z",
                "days": 1.0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pass_count"] == 4



def test_api_passes_endpoint_not_found():
    """Verify 404 is returned if the requested satellite does not exist."""
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = None

        response = client.get(
            "/api/orbit/satellite/99999/passes",
            params={"lat": 28.6139, "lon": 77.2090},
        )
        assert response.status_code == 404


def test_api_passes_endpoint_validation_error():
    """Verify 422 is returned for invalid query parameters."""
    response = client.get(
        "/api/orbit/satellite/25544/passes",
        params={"lat": 95.0, "lon": 77.2090},  # lat > 90
    )
    assert response.status_code == 422


def test_api_passes_endpoint_end_before_start():
    """Verify 400 is returned if end_time <= start_time."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/passes",
            params={
                "lat": 28.6139,
                "lon": 77.2090,
                "start_time": "2026-11-15T12:00:00Z",
                "end_time": "2026-11-15T10:00:00Z",
            },
        )
        assert response.status_code == 400
        assert "strictly after start_time" in response.json()["detail"].lower()


def test_near_tangent_grazing_pass_regression_15_847530():
    """Task 1.4.1 Regression: Verify near-tangent grazing pass root-finding converges with < 1e-4 deg residual."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Pass 4 peaks at 15.856371 deg between coarse samples at 16:07:00 (15.757 deg) and 16:07:20 (15.839 deg).
    # Coarse samples are both below threshold 15.847530 deg.
    threshold = 15.847530
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T16:02:00Z",
        "2026-11-15T16:12:00Z",
        min_elevation_deg=threshold,
    )

    assert result.pass_count == 1
    p = result.passes[0]

    # Verify physical ordering: AOS < Culmination < LOS
    dt_aos = datetime.fromisoformat(p.aos.time)
    dt_culm = datetime.fromisoformat(p.max_elevation.time)
    dt_los = datetime.fromisoformat(p.los.time)
    assert dt_aos < dt_culm < dt_los

    # Verify timestamps lie within the expected coarse interval [16:07:00, 16:07:20]
    dt_left = datetime(2026, 11, 15, 16, 7, 0, tzinfo=timezone.utc)
    dt_right = datetime(2026, 11, 15, 16, 7, 20, tzinfo=timezone.utc)
    assert dt_left <= dt_aos <= dt_right
    assert dt_left <= dt_los <= dt_right

    # Evaluate exact elevation at refined AOS and LOS
    ts = load.timescale(builtin=True)
    sat = EarthSatellite.from_omm(ts, record.to_omm_dict())
    ground = wgs84.latlon(observer.latitude, observer.longitude, observer.elevation_m)
    diff = sat - ground

    t_aos_sky = ts.from_datetime(dt_aos)
    t_los_sky = ts.from_datetime(dt_los)

    alt_aos = diff.at(t_aos_sky).altaz(temperature_C=None)[0].degrees
    alt_los = diff.at(t_los_sky).altaz(temperature_C=None)[0].degrees

    # Elevation residuals must be strictly below 1e-4 degrees
    res_aos = abs(alt_aos - threshold)
    res_los = abs(alt_los - threshold)
    assert res_aos < 1e-4, f"AOS elevation residual {res_aos} exceeded 1e-4 deg"
    assert res_los < 1e-4, f"LOS elevation residual {res_los} exceeded 1e-4 deg"


def test_near_tangent_peak_just_below_threshold_rejected():
    """Task 1.4.1 Regression: Verify peak just below threshold (15.86 deg > 15.856 deg) is rejected."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = PassPredictionService()

    # Peak is at 15.856371 deg; threshold 15.860000 deg is just above the peak
    result = service.predict_passes(
        record,
        observer,
        "2026-11-15T16:02:00Z",
        "2026-11-15T16:12:00Z",
        min_elevation_deg=15.860000,
    )

    assert result.pass_count == 0
    assert result.passes == []

