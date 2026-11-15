"""Topocentric observer calculations for satellite tracking."""

from datetime import datetime, timezone
import math
from typing import Union
from sgp4.api import SGP4_ERRORS
from skyfield.api import EarthSatellite, load, wgs84

from app.orbit.exceptions import ObserverError, PropagationError
from app.orbit.models import ObserverLocation, OrbitalRecord, TopocentricResult, Vector3D


class ObserverService:
    """Service for calculating topocentric observer geometry and satellite appearance."""

    def __init__(self):
        # Builtin timescale runs 100% offline without remote ephemeris downloads
        self.ts = load.timescale(builtin=True)

    def observe(
        self,
        record: OrbitalRecord,
        observer: ObserverLocation,
        target_time: Union[datetime, str],
    ) -> TopocentricResult:
        """Calculate topocentric coordinates (azimuth, elevation, slant range) of a satellite.

        Args:
            record: Validated OrbitalRecord element set.
            observer: ObserverLocation specifying WGS84 latitude, longitude, and elevation.
            target_time: Target timestamp as timezone-aware datetime or ISO 8601 string.

        Returns:
            TopocentricResult containing azimuth, geometric elevation, slant range, range rate,
            and relative state vectors.

        Raises:
            PropagationError: If timestamp is invalid or SGP4 propagation encounters an error.
            ObserverError: If observer coordinates are invalid.
        """
        dt = self._parse_target_time(target_time)

        try:
            ground_station = wgs84.latlon(
                latitude_degrees=observer.latitude,
                longitude_degrees=observer.longitude,
                elevation_m=observer.elevation_m,
            )
        except Exception as exc:
            raise ObserverError(f"Invalid observer coordinates: {exc}") from exc

        try:
            omm_dict = record.to_omm_dict()
            sat = EarthSatellite.from_omm(self.ts, omm_dict)
        except Exception as exc:
            raise PropagationError(
                f"Failed to initialize SGP4 satellite model for NORAD ID {record.norad_cat_id}: {exc}"
            ) from exc

        try:
            t = self.ts.from_datetime(dt)
            # Difference vector: satellite relative to observer
            difference = sat - ground_station
            topocentric = difference.at(t)
        except Exception as exc:
            raise PropagationError(
                f"Topocentric calculation failed at {dt.isoformat()}: {exc}"
            ) from exc

        # Check internal SGP4 status code
        if sat.model.error != 0:
            err_code = sat.model.error
            err_msg = getattr(
                sat.model, "error_message", SGP4_ERRORS.get(err_code, f"Error {err_code}")
            )
            raise PropagationError(
                f"SGP4 propagation error for NORAD ID {record.norad_cat_id} ({err_code}): {err_msg}"
            )

        # Compute geometric alt/az without atmospheric refraction
        # Setting temperature_C=None explicitly disables refraction correction in Skyfield
        alt, az, distance = topocentric.altaz(temperature_C=None)

        pos_km = topocentric.position.km
        vel_km_s = topocentric.velocity.km_per_s

        # Verify numerical sanity (no NaNs or infinities)
        if (
            math.isnan(alt.degrees)
            or math.isnan(az.degrees)
            or math.isnan(distance.km)
            or any(math.isnan(v) or math.isinf(v) for v in pos_km)
            or any(math.isnan(v) or math.isinf(v) for v in vel_km_s)
        ):
            raise PropagationError(
                f"SGP4 produced non-finite topocentric coordinates at {dt.isoformat()}"
            )

        # Range rate: dot product of relative position and relative velocity / range
        # Negative means approaching (distance decreasing), positive means receding
        dist_val = float(distance.km)
        if dist_val > 0:
            range_rate = float(
                (pos_km[0] * vel_km_s[0] + pos_km[1] * vel_km_s[1] + pos_km[2] * vel_km_s[2])
                / dist_val
            )
        else:
            range_rate = 0.0

        return TopocentricResult(
            norad_cat_id=record.norad_cat_id,
            object_name=record.object_name,
            timestamp=dt.isoformat(),
            observer=observer,
            azimuth_deg=float(az.degrees),
            elevation_deg=float(alt.degrees),
            slant_range_km=dist_val,
            range_rate_km_per_s=range_rate,
            relative_position_km=Vector3D(
                x=float(pos_km[0]),
                y=float(pos_km[1]),
                z=float(pos_km[2]),
            ),
            relative_velocity_km_per_s=Vector3D(
                x=float(vel_km_s[0]),
                y=float(vel_km_s[1]),
                z=float(vel_km_s[2]),
            ),
        )

    def _parse_target_time(self, target_time: Union[datetime, str]) -> datetime:
        """Parse and normalize target time to UTC datetime."""
        if isinstance(target_time, str):
            cleaned = target_time.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(cleaned)
            except Exception as exc:
                raise PropagationError(
                    f"Invalid ISO 8601 timestamp string '{target_time}': {exc}"
                ) from exc
        elif isinstance(target_time, datetime):
            dt = target_time
        else:
            raise PropagationError(
                f"Target time must be datetime or ISO 8601 string, got {type(target_time)}"
            )

        # Enforce UTC timezone awareness
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt
