from datetime import datetime, timezone
import math
from typing import Union
from sgp4.api import SGP4_ERRORS
from skyfield.api import EarthSatellite, load

from app.orbit.exceptions import PropagationError
from app.orbit.models import OrbitalRecord, PropagationResult, Vector3D


class OrbitPropagator:
    """Independent SGP4 propagation service for calculating satellite position and velocity."""

    def __init__(self):
        # Builtin timescale runs 100% offline without remote ephemeris downloads
        self.ts = load.timescale(builtin=True)

    def propagate(
        self, record: OrbitalRecord, target_time: Union[datetime, str]
    ) -> PropagationResult:
        """Propagate an orbital record to a specified UTC timestamp using SGP4.

        Args:
            record: Validated OrbitalRecord element set.
            target_time: Target timestamp as a timezone-aware datetime or ISO 8601 string.

        Returns:
            PropagationResult containing GCRS position (km) and velocity (km/s).

        Raises:
            PropagationError: If timestamp is invalid or SGP4 propagation encounters an error.
        """
        dt = self._parse_target_time(target_time)

        try:
            omm_dict = record.to_omm_dict()
            sat = EarthSatellite.from_omm(self.ts, omm_dict)
        except Exception as exc:
            raise PropagationError(
                f"Failed to initialize SGP4 satellite model for NORAD ID {record.norad_cat_id}: {exc}"
            ) from exc

        try:
            t = self.ts.from_datetime(dt)
            geocentric = sat.at(t)
        except Exception as exc:
            raise PropagationError(
                f"Skyfield propagation failed at {dt.isoformat()}: {exc}"
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

        pos_km = geocentric.position.km
        vel_km_s = geocentric.velocity.km_per_s

        # Verify numerical sanity (no NaNs or infinities)
        if (
            any(math.isnan(v) or math.isinf(v) for v in pos_km)
            or any(math.isnan(v) or math.isinf(v) for v in vel_km_s)
        ):
            raise PropagationError(
                f"SGP4 produced non-finite state vectors at {dt.isoformat()}"
            )

        return PropagationResult(
            norad_cat_id=record.norad_cat_id,
            object_name=record.object_name,
            timestamp=dt.isoformat(),
            coordinate_frame="GCRS",
            position_km=Vector3D(
                x=float(pos_km[0]), y=float(pos_km[1]), z=float(pos_km[2])
            ),
            velocity_km_per_s=Vector3D(
                x=float(vel_km_s[0]), y=float(vel_km_s[1]), z=float(vel_km_s[2])
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
