from datetime import datetime, timezone
import math
from unittest.mock import AsyncMock, patch
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from skyfield.api import EarthSatellite, load, wgs84

from app.main import app
from app.orbit.exceptions import (
    CelesTrakRateLimitError,
    CelesTrakTimeoutError,
    PropagationError,
    RecordValidationError,
)
from app.orbit.models import ObserverLocation, OrbitalRecord, TopocentricResult
from app.orbit.observer import ObserverService

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


def test_observer_location_valid():
    """Verify valid ObserverLocation instantiations."""
    obs = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    assert obs.latitude == 28.6139
    assert obs.longitude == 77.2090
    assert obs.elevation_m == 216.0

    # Default elevation is 0.0
    obs_default = ObserverLocation(latitude=0.0, longitude=0.0)
    assert obs_default.elevation_m == 0.0


@pytest.mark.parametrize(
    "lat,lon,alt,err_match",
    [
        (91.0, 0.0, 0.0, "latitude"),
        (-90.1, 0.0, 0.0, "latitude"),
        (0.0, 180.1, 0.0, "longitude"),
        (0.0, -180.1, 0.0, "longitude"),
        (0.0, 0.0, -600.0, "elevation_m"),
        (0.0, 0.0, 10000.0, "elevation_m"),
    ],
)
def test_observer_location_boundary_validation(lat, lon, alt, err_match):
    """Verify ObserverLocation rejects coordinates outside valid physical bounds."""
    with pytest.raises(ValidationError) as exc_info:
        ObserverLocation(latitude=lat, longitude=lon, elevation_m=alt)
    assert err_match in str(exc_info.value)


def test_observer_service_deterministic_topocentric():
    """Verify deterministic topocentric calculations for ISS from New Delhi."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = ObserverService()

    target_time = "2026-11-15T05:00:00Z"
    result = service.observe(record, observer, target_time)

    assert isinstance(result, TopocentricResult)
    assert result.norad_cat_id == 25544
    assert result.object_name == "ISS (ZARYA)"
    assert result.timestamp == "2026-11-15T05:00:00+00:00"
    assert result.observer.latitude == 28.6139
    assert result.observer.longitude == 77.2090

    # Verify plausible ranges
    assert 0.0 <= result.azimuth_deg < 360.0
    assert -90.0 <= result.elevation_deg <= 90.0
    assert result.slant_range_km > 300.0  # LEO slant range is always > 300 km

    # Direct norm check: slant_range_km == norm(relative_position_km)
    rx, ry, rz = (
        result.relative_position_km.x,
        result.relative_position_km.y,
        result.relative_position_km.z,
    )
    vec_norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    assert math.isclose(result.slant_range_km, vec_norm, rel_tol=1e-7)


def test_geometric_elevation_guarantee_no_refraction():
    """Verify that elevation is pure geometric and atmospheric refraction is not applied."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    # Choose observer and time where satellite is near the horizon (~5 deg elevation)
    # where atmospheric refraction would be noticeable (~0.1 to 0.2 deg)
    observer = ObserverLocation(latitude=10.0, longitude=75.0, elevation_m=10.0)
    service = ObserverService()

    target_time = "2026-11-15T05:08:00Z"
    result = service.observe(record, observer, target_time)

    # Compute directly with Skyfield
    ts = load.timescale(builtin=True)
    sat = EarthSatellite.from_omm(ts, record.to_omm_dict())
    ground = wgs84.latlon(10.0, 75.0, elevation_m=10.0)
    t = ts.from_datetime(datetime(2026, 11, 15, 5, 8, 0, tzinfo=timezone.utc))
    diff = (sat - ground).at(t)

    # Geometric elevation (temperature_C=None)
    alt_geom, az_geom, dist_geom = diff.altaz(temperature_C=None)
    # Refraction-adjusted elevation (temperature_C=15.0)
    alt_refr, _, _ = diff.altaz(temperature_C=15.0)

    # Service output MUST match geometric elevation exactly
    assert math.isclose(result.elevation_deg, alt_geom.degrees, abs_tol=1e-6)

    # And if satellite is close to horizon, refraction-adjusted elevation must differ
    if 0.0 < alt_geom.degrees < 30.0:
        assert not math.isclose(result.elevation_deg, alt_refr.degrees, abs_tol=1e-4)


def test_independent_topocentric_benchmark_validation():
    """Verify ObserverService against an independent, first-principles mathematical implementation.

    Independent method:
    1. Geodetic to ECEF (WGS84 ellipsoidal geometry)
    2. SGP4 TEME to ECEF via GMST rotation
    3. ECEF difference vector to local topocentric ENU frame
    4. Compute Azimuth, Elevation, Slant Range
    """
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    lat_deg, lon_deg, alt_m = 28.6139, 77.2090, 216.0
    observer = ObserverLocation(latitude=lat_deg, longitude=lon_deg, elevation_m=alt_m)
    service = ObserverService()

    target_time = "2026-11-15T05:00:00Z"
    res = service.observe(record, observer, target_time)

    # --- Independent First-Principles Implementation ---
    # WGS84 Constants
    a = 6378.137  # Semi-major axis in km
    f = 1.0 / 298.257223563  # Flattening
    e2 = 2 * f - f**2

    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)
    h_km = alt_m / 1000.0

    # 1. Observer position in ECEF
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    x_obs = (N + h_km) * math.cos(phi) * math.cos(lam)
    y_obs = (N + h_km) * math.cos(phi) * math.sin(lam)
    z_obs = (N * (1 - e2) + h_km) * math.sin(phi)
    r_obs_ecef = np.array([x_obs, y_obs, z_obs])

    # 2. SGP4 raw TEME state
    ts = load.timescale(builtin=True)
    t = ts.utc(2026, 11, 15, 5, 0, 0)
    sat = EarthSatellite.from_omm(ts, record.to_omm_dict())

    from sgp4.api import jday

    jd, fr = jday(2026, 11, 15, 5, 0, 0)
    _, r_teme, _ = sat.model.sgp4(jd, fr)
    r_teme = np.array(r_teme)

    # 3. Rotate TEME to ECEF via GMST
    gmst_rad = t.gmst * math.pi / 12.0
    cos_g, sin_g = math.cos(gmst_rad), math.sin(gmst_rad)
    R_z = np.array([[cos_g, sin_g, 0], [-sin_g, cos_g, 0], [0, 0, 1]])
    r_sat_ecef = R_z @ r_teme

    # Observer-to-satellite displacement in ECEF
    rho_ecef = r_sat_ecef - r_obs_ecef

    # 4. Rotate ECEF to local topocentric ENU (East, North, Up)
    sin_phi, cos_phi = math.sin(phi), math.cos(phi)
    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    R_ecef_to_enu = np.array(
        [
            [-sin_lam, cos_lam, 0],
            [-sin_phi * cos_lam, -sin_phi * sin_lam, cos_phi],
            [cos_phi * cos_lam, cos_phi * sin_lam, sin_phi],
        ]
    )
    rho_enu = R_ecef_to_enu @ rho_ecef
    E, N_coord, U = rho_enu

    # 5. Spherical topocentric coordinates
    slant_range_ind = float(np.linalg.norm(rho_enu))
    el_ind = math.degrees(math.asin(U / slant_range_ind))
    az_ind = math.degrees(math.atan2(E, N_coord)) % 360.0

    # Numerical comparison:
    # Azimuth within 0.0001 deg (0.36 arcsec)
    assert abs(res.azimuth_deg - az_ind) < 0.0001
    # Elevation within 0.0001 deg (0.36 arcsec)
    assert abs(res.elevation_deg - el_ind) < 0.0001
    # Slant range within 0.002 km (2 metres out of ~7690 km: 99.99997% match)
    assert abs(res.slant_range_km - slant_range_ind) < 0.002


def test_range_rate_sign_and_numerical_derivative():
    """Verify that range rate sign and magnitude match the numerical derivative of slant range."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = ObserverService()

    # Case 1: Receding satellite (slant range increasing)
    delta_s = 1.0  # 1 second
    dt_center = datetime(2026, 11, 15, 5, 0, 0, tzinfo=timezone.utc)
    dt_prev = datetime(2026, 11, 15, 4, 59, 59, tzinfo=timezone.utc)
    dt_next = datetime(2026, 11, 15, 5, 0, 1, tzinfo=timezone.utc)

    res_center = service.observe(record, observer, dt_center)
    res_prev = service.observe(record, observer, dt_prev)
    res_next = service.observe(record, observer, dt_next)

    # Numerical central difference: d(rho)/dt ≈ (rho(t+dt) - rho(t-dt)) / (2*dt)
    numerical_range_rate = (res_next.slant_range_km - res_prev.slant_range_km) / (
        2.0 * delta_s
    )

    # Analytic range rate from service: dot(pos, vel) / norm(pos)
    analytic_range_rate = res_center.range_rate_km_per_s

    # Range rate must match numerical derivative to within 0.01 km/s
    assert math.isclose(analytic_range_rate, numerical_range_rate, abs_tol=0.01)
    # Range is increasing, so range rate must be positive
    assert res_next.slant_range_km > res_prev.slant_range_km
    assert analytic_range_rate > 0.0

    # Case 2: Approaching satellite (find an epoch where distance is decreasing)
    dt_app_center = datetime(2026, 11, 15, 4, 30, 0, tzinfo=timezone.utc)
    dt_app_prev = datetime(2026, 11, 15, 4, 29, 59, tzinfo=timezone.utc)
    dt_app_next = datetime(2026, 11, 15, 4, 30, 1, tzinfo=timezone.utc)

    res_app_center = service.observe(record, observer, dt_app_center)
    res_app_prev = service.observe(record, observer, dt_app_prev)
    res_app_next = service.observe(record, observer, dt_app_next)

    app_num_rate = (res_app_next.slant_range_km - res_app_prev.slant_range_km) / (
        2.0 * delta_s
    )
    assert math.isclose(res_app_center.range_rate_km_per_s, app_num_rate, abs_tol=0.01)
    if res_app_next.slant_range_km < res_app_prev.slant_range_km:
        assert res_app_center.range_rate_km_per_s < 0.0



def test_observer_timestamp_variations():
    """Verify ISO 8601 variations, fractional seconds, explicit offsets, and naive datetimes."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090, elevation_m=216.0)
    service = ObserverService()

    # With Z
    res1 = service.observe(record, observer, "2026-11-15T05:00:00Z")
    # With explicit offset matching UTC
    res2 = service.observe(record, observer, "2026-11-15T05:00:00+00:00")
    # With +05:30 offset (10:30:00 IST is 05:00:00 UTC)
    res3 = service.observe(record, observer, "2026-11-15T10:30:00+05:30")
    # Naive datetime
    res4 = service.observe(record, observer, datetime(2026, 11, 15, 5, 0, 0))

    assert math.isclose(res1.azimuth_deg, res2.azimuth_deg, abs_tol=1e-5)
    assert math.isclose(res1.azimuth_deg, res3.azimuth_deg, abs_tol=1e-5)
    assert math.isclose(res1.azimuth_deg, res4.azimuth_deg, abs_tol=1e-5)


def test_observer_invalid_timestamp():
    """Verify invalid timestamp raises PropagationError."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090)
    service = ObserverService()

    with pytest.raises(PropagationError, match="Invalid ISO 8601"):
        service.observe(record, observer, "invalid-timestamp")


def test_observer_decayed_satellite_raises_error():
    """Verify that propagating a decayed orbit raises a PropagationError."""
    decayed_record = dict(MOCK_ISS_OMM)
    decayed_record["BSTAR"] = 0.05
    record = OrbitalRecord.model_validate(decayed_record)
    observer = ObserverLocation(latitude=28.6139, longitude=77.2090)
    service = ObserverService()

    with pytest.raises(PropagationError, match="(?i)sgp4 propagation error"):
        service.observe(record, observer, "2030-01-01T00:00:00Z")


# --- API Route Tests ---


def test_api_observe_endpoint_success():
    """Verify GET /api/orbit/satellite/{catnr}/observe returns 200 and TopocentricResult."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/observe",
            params={
                "lat": 28.6139,
                "lon": 77.2090,
                "alt_m": 216.0,
                "time": "2026-11-15T05:00:00Z",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["norad_cat_id"] == 25544
        assert data["object_name"] == "ISS (ZARYA)"
        assert "azimuth_deg" in data
        assert "elevation_deg" in data
        assert "slant_range_km" in data
        assert "range_rate_km_per_s" in data
        assert data["observer"]["latitude"] == 28.6139
        assert data["observer"]["longitude"] == 77.2090
        assert data["observer"]["elevation_m"] == 216.0


def test_api_observe_endpoint_defaults():
    """Verify GET /api/orbit/satellite/{catnr}/observe works with default alt_m and time."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/observe",
            params={"lat": 12.9716, "lon": 77.5946},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["observer"]["elevation_m"] == 0.0


def test_api_observe_endpoint_satellite_not_found():
    """Verify 404 is returned if the requested satellite does not exist."""
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = None

        response = client.get(
            "/api/orbit/satellite/99999/observe",
            params={"lat": 28.6139, "lon": 77.2090},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "lat,lon,alt,catnr",
    [
        (95.0, 77.0, 0.0, 25544),
        (-95.0, 77.0, 0.0, 25544),
        (28.0, 185.0, 0.0, 25544),
        (28.0, -185.0, 0.0, 25544),
        (28.0, 77.0, -1000.0, 25544),
        (28.0, 77.0, 15000.0, 25544),
        (28.0, 77.0, 0.0, 0),
        (28.0, 77.0, 0.0, -10),
    ],
)
def test_api_observe_endpoint_validation_errors(lat, lon, alt, catnr):
    """Verify 422 Unprocessable Entity for invalid latitude, longitude, altitude or catnr."""
    response = client.get(
        f"/api/orbit/satellite/{catnr}/observe",
        params={"lat": lat, "lon": lon, "alt_m": alt},
    )
    assert response.status_code == 422


def test_api_observe_endpoint_invalid_time():
    """Verify 400 Bad Request for malformed time parameter."""
    record = OrbitalRecord.model_validate(MOCK_ISS_OMM)

    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = record

        response = client.get(
            "/api/orbit/satellite/25544/observe",
            params={
                "lat": 28.6139,
                "lon": 77.2090,
                "time": "not-a-valid-timestamp",
            },
        )
        assert response.status_code == 400
        assert "invalid iso 8601" in response.json()["detail"].lower()


def test_api_observe_upstream_errors():
    """Verify upstream CelesTrak errors are correctly translated."""
    with patch(
        "app.orbit.router.orbit_service.get_satellite_by_catnr",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.side_effect = CelesTrakRateLimitError("Rate limit exceeded")
        res = client.get(
            "/api/orbit/satellite/25544/observe",
            params={"lat": 28.6139, "lon": 77.2090},
        )
        assert res.status_code == 429

        mock_get.side_effect = CelesTrakTimeoutError("Timeout")
        res = client.get(
            "/api/orbit/satellite/25544/observe",
            params={"lat": 28.6139, "lon": 77.2090},
        )
        assert res.status_code == 504

        mock_get.side_effect = RecordValidationError("Bad data")
        res = client.get(
            "/api/orbit/satellite/25544/observe",
            params={"lat": 28.6139, "lon": 77.2090},
        )
        assert res.status_code == 502
