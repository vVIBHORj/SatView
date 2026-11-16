"""Satellite pass prediction service for calculating visible ground passes."""

from datetime import datetime, timedelta, timezone
import math
from typing import Any, Optional, Union
import numpy as np
from sgp4.api import SGP4_ERRORS
from skyfield.api import EarthSatellite, load, wgs84

from app.orbit.exceptions import ObserverError, PassPredictionError, PropagationError
from app.orbit.models import (
    ClosestApproachPoint,
    ObserverLocation,
    OrbitalRecord,
    PassPoint,
    PassPredictionResult,
    SatellitePass,
)
from app.orbit.observer import ObserverService


class PassPredictionService:
    """Service for predicting satellite visibility windows and passes over a ground station."""

    def __init__(self, observer_service: Optional[ObserverService] = None):
        # Builtin timescale runs 100% offline without remote ephemeris downloads
        self.ts = load.timescale(builtin=True)
        self.observer_service = observer_service or ObserverService()

    def predict_passes(
        self,
        record: OrbitalRecord,
        observer: ObserverLocation,
        start_time: Union[datetime, str],
        end_time: Union[datetime, str],
        min_elevation_deg: float = 10.0,
    ) -> PassPredictionResult:
        """Predict all satellite passes above the minimum elevation threshold within a time window.

        Args:
            record: Validated OrbitalRecord element set.
            observer: ObserverLocation on WGS84 ellipsoid.
            start_time: Start of observation window (datetime or ISO 8601 string).
            end_time: End of observation window (datetime or ISO 8601 string).
            min_elevation_deg: Minimum elevation threshold in degrees (default 10.0).

        Returns:
            PassPredictionResult containing list of predicted SatellitePass objects.

        Raises:
            PassPredictionError: If time window or elevation threshold is invalid.
            ObserverError: If observer coordinates are invalid.
            PropagationError: If SGP4 propagation fails.
        """
        dt_start = self._parse_target_time(start_time)
        dt_end = self._parse_target_time(end_time)

        if dt_end <= dt_start:
            raise PassPredictionError(
                f"end_time ({dt_end.isoformat()}) must be strictly after start_time ({dt_start.isoformat()})"
            )

        window_duration_s = (dt_end - dt_start).total_seconds()
        max_window_s = 14 * 86400.0  # 14 days maximum
        if window_duration_s > max_window_s:
            raise PassPredictionError(
                f"Observation window duration ({window_duration_s / 86400.0:.1f} days) "
                f"exceeds maximum allowable limit of 14 days"
            )

        if not (0.0 <= min_elevation_deg <= 85.0):
            raise PassPredictionError(
                f"min_elevation_deg must be between 0.0 and 85.0 degrees, got {min_elevation_deg}"
            )

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

        # Verify satellite is not decayed at window start
        t_start_sky = self.ts.from_datetime(dt_start)
        sat.at(t_start_sky)
        if sat.model.error != 0:
            err_code = sat.model.error
            err_msg = getattr(
                sat.model, "error_message", SGP4_ERRORS.get(err_code, f"Error {err_code}")
            )
            raise PropagationError(
                f"SGP4 propagation error at window start for NORAD ID {record.norad_cat_id} ({err_code}): {err_msg}"
            )

        diff_obj = sat - ground_station

        # Adaptive step size: 20s for LEO, up to 60s for higher orbits
        mean_motion = max(record.mean_motion, 0.1)
        period_s = 86400.0 / mean_motion
        step_s = 20.0 if period_s < 7200.0 else min(60.0, max(20.0, period_s / 120.0))

        sec_arr = np.arange(0.0, window_duration_s + step_s, step_s)
        if sec_arr[-1] < window_duration_s:
            sec_arr = np.append(sec_arr, window_duration_s)
        elif sec_arr[-1] > window_duration_s:
            sec_arr[-1] = window_duration_s

        # Vectorized coarse elevation evaluation across entire window
        times = self.ts.utc(
            dt_start.year,
            dt_start.month,
            dt_start.day,
            dt_start.hour,
            dt_start.minute,
            dt_start.second + sec_arr + dt_start.microsecond / 1e6,
        )

        try:
            coarse_topo = diff_obj.at(times)
            coarse_alts = coarse_topo.altaz(temperature_C=None)[0].degrees
        except Exception as exc:
            raise PropagationError(f"Pass scan propagation failed: {exc}") from exc

        # Edge Case: Geostationary / Continuous Visibility
        above = coarse_alts >= min_elevation_deg
        if np.all(above):
            single_pass = self._build_continuous_pass(
                diff_obj, dt_start, dt_end, window_duration_s
            )
            return PassPredictionResult(
                norad_cat_id=record.norad_cat_id,
                object_name=record.object_name,
                observer=observer,
                start_time=dt_start.isoformat(),
                end_time=dt_end.isoformat(),
                min_elevation_deg=min_elevation_deg,
                pass_count=1,
                passes=[single_pass],
            )

        # Detect candidate pass segments (runs of above == True)
        segments: list[tuple[int, int]] = []
        in_segment = False
        start_idx = 0

        for i in range(len(coarse_alts)):
            if above[i] and not in_segment:
                in_segment = True
                start_idx = i
            elif not above[i] and in_segment:
                in_segment = False
                segments.append((start_idx, i))

        if in_segment:
            segments.append((start_idx, len(coarse_alts) - 1))

        # Check for grazing crests that didn't cross threshold in coarse scan
        # (local maxima where peak is near min_elevation_deg)
        for i in range(1, len(coarse_alts) - 1):
            if (
                not above[i]
                and coarse_alts[i] >= (min_elevation_deg - 2.5)
                and coarse_alts[i] > coarse_alts[i - 1]
                and coarse_alts[i] > coarse_alts[i + 1]
            ):
                # Optimize to find true peak
                s_peak = self._golden_section_opt(
                    lambda s: self._eval_alt(diff_obj, dt_start, s),
                    sec_arr[i - 1],
                    sec_arr[i + 1],
                    maximize=True,
                )
                alt_peak = self._eval_alt(diff_obj, dt_start, s_peak)
                if alt_peak >= min_elevation_deg:
                    # Found grazing pass; check it doesn't overlap existing segments
                    overlap = any(
                        sec_arr[s_start] <= s_peak <= sec_arr[s_end]
                        for s_start, s_end in segments
                    )
                    if not overlap:
                        segments.append((max(0, i - 1), min(len(coarse_alts) - 1, i + 1)))

        # Sort candidate segments chronologically
        segments.sort(key=lambda seg: sec_arr[seg[0]])

        # Refine each candidate segment into a SatellitePass
        passes: list[SatellitePass] = []
        pass_idx = 1

        for s_start_i, s_end_i in segments:
            pass_obj = self._refine_pass(
                pass_id=pass_idx,
                diff_obj=diff_obj,
                dt_start=dt_start,
                sec_arr=sec_arr,
                coarse_alts=coarse_alts,
                start_i=s_start_i,
                end_i=s_end_i,
                min_elevation_deg=min_elevation_deg,
                window_duration_s=window_duration_s,
            )
            if pass_obj is not None:
                passes.append(pass_obj)
                pass_idx += 1

        return PassPredictionResult(
            norad_cat_id=record.norad_cat_id,
            object_name=record.object_name,
            observer=observer,
            start_time=dt_start.isoformat(),
            end_time=dt_end.isoformat(),
            min_elevation_deg=min_elevation_deg,
            pass_count=len(passes),
            passes=passes,
        )

    def _refine_pass(
        self,
        pass_id: int,
        diff_obj: Any,
        dt_start: datetime,
        sec_arr: np.ndarray,
        coarse_alts: np.ndarray,
        start_i: int,
        end_i: int,
        min_elevation_deg: float,
        window_duration_s: float,
    ) -> Optional[SatellitePass]:
        """Refine a coarse candidate interval into an accurate SatellitePass using root finding."""

        # 1. Refine Culmination (Maximum Elevation) first to establish the above-threshold point
        s_search_a = sec_arr[max(0, start_i - 1)] if start_i > 0 else 0.0
        s_search_b = sec_arr[end_i] if end_i < len(sec_arr) else window_duration_s
        s_culm = self._golden_section_opt(
            lambda s: self._eval_alt(diff_obj, dt_start, s),
            s_search_a,
            s_search_b,
            maximize=True,
            tol=1e-3,
        )
        alt_culm, az_culm, dist_culm = self._eval_all(diff_obj, dt_start, s_culm)

        # Discard candidate if true maximum elevation does not reach threshold
        if alt_culm < min_elevation_deg:
            return None

        f_culm = alt_culm - min_elevation_deg

        # 2. Refine AOS (Rise time)
        truncated_start = False
        if start_i == 0 and coarse_alts[0] >= min_elevation_deg:
            s_aos = 0.0
            truncated_start = True
        else:
            bracket_a = sec_arr[max(0, start_i - 1)]
            bracket_b = sec_arr[start_i]
            fa = self._eval_alt(diff_obj, dt_start, bracket_a) - min_elevation_deg
            fb = self._eval_alt(diff_obj, dt_start, bracket_b) - min_elevation_deg

            # Check if standard coarse bracket spans the crossing
            if fa * fb <= 0:
                s_aos = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    bracket_a,
                    bracket_b,
                    tol=1e-4,
                )
            # Grazing crest case: coarse samples below threshold, bracket with culmination
            elif fa < 0 and f_culm >= 0:
                s_aos = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    bracket_a,
                    s_culm,
                    tol=1e-4,
                )
            elif fb < 0 and f_culm >= 0:
                s_aos = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    bracket_b,
                    s_culm,
                    tol=1e-4,
                )
            else:
                return None

        # 3. Refine LOS (Set time)
        truncated_end = False
        if end_i == len(coarse_alts) - 1 and coarse_alts[-1] >= min_elevation_deg:
            s_los = window_duration_s
            truncated_end = True
        else:
            bracket_c = sec_arr[max(0, end_i - 1)]
            bracket_d = sec_arr[end_i]
            fc = self._eval_alt(diff_obj, dt_start, bracket_c) - min_elevation_deg
            fd = self._eval_alt(diff_obj, dt_start, bracket_d) - min_elevation_deg

            # Check if standard coarse bracket spans the crossing
            if fc * fd <= 0:
                s_los = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    bracket_c,
                    bracket_d,
                    tol=1e-4,
                )
            # Grazing crest case: coarse samples below threshold, bracket with culmination
            elif f_culm >= 0 and fd < 0:
                s_los = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    s_culm,
                    bracket_d,
                    tol=1e-4,
                )
            elif f_culm >= 0 and fc < 0:
                s_los = self._brent_root(
                    lambda s: self._eval_alt(diff_obj, dt_start, s) - min_elevation_deg,
                    s_culm,
                    bracket_c,
                    tol=1e-4,
                )
            else:
                return None

        if s_los <= s_aos:
            return None

        # Ensure culmination lies strictly within [s_aos, s_los]
        if not (s_aos <= s_culm <= s_los):
            s_culm = self._golden_section_opt(
                lambda s: self._eval_alt(diff_obj, dt_start, s),
                s_aos,
                s_los,
                maximize=True,
                tol=1e-3,
            )
            alt_culm, az_culm, dist_culm = self._eval_all(diff_obj, dt_start, s_culm)
            if alt_culm < min_elevation_deg:
                return None

        # 4. Find Time of Closest Approach (TCA / Minimum Slant Range)
        # Note: In eccentric orbits, TCA does NOT always coincide with maximum elevation
        s_tca = self._golden_section_opt(
            lambda s: self._eval_range(diff_obj, dt_start, s),
            s_aos,
            s_los,
            maximize=False,
            tol=1e-3,
        )
        alt_tca, az_tca, dist_tca = self._eval_all(diff_obj, dt_start, s_tca)

        # Evaluate coordinates at AOS and LOS
        alt_aos, az_aos, dist_aos = self._eval_all(diff_obj, dt_start, s_aos)
        alt_los, az_los, dist_los = self._eval_all(diff_obj, dt_start, s_los)

        dt_aos = dt_start + timedelta(seconds=s_aos)
        dt_los = dt_start + timedelta(seconds=s_los)
        dt_culm = dt_start + timedelta(seconds=s_culm)
        dt_tca = dt_start + timedelta(seconds=s_tca)

        duration_s = max(0.0, s_los - s_aos)

        return SatellitePass(
            pass_id=pass_id,
            aos=PassPoint(
                time=dt_aos.isoformat(),
                azimuth_deg=round(az_aos, 4),
                elevation_deg=round(alt_aos, 4),
                slant_range_km=round(dist_aos, 4),
            ),
            los=PassPoint(
                time=dt_los.isoformat(),
                azimuth_deg=round(az_los, 4),
                elevation_deg=round(alt_los, 4),
                slant_range_km=round(dist_los, 4),
            ),
            max_elevation=PassPoint(
                time=dt_culm.isoformat(),
                azimuth_deg=round(az_culm, 4),
                elevation_deg=round(alt_culm, 4),
                slant_range_km=round(dist_culm, 4),
            ),
            closest_approach=ClosestApproachPoint(
                time=dt_tca.isoformat(),
                slant_range_km=round(dist_tca, 4),
                elevation_deg=round(alt_tca, 4),
                azimuth_deg=round(az_tca, 4),
            ),
            duration_s=round(duration_s, 2),
            is_complete=not (truncated_start or truncated_end),
            is_continuous=False,
        )

    def _build_continuous_pass(
        self,
        diff_obj: Any,
        dt_start: datetime,
        dt_end: datetime,
        window_duration_s: float,
    ) -> SatellitePass:
        """Handle continuously visible satellite across entire observation window (e.g. GEO)."""
        # Find maximum elevation across window
        s_culm = self._golden_section_opt(
            lambda s: self._eval_alt(diff_obj, dt_start, s),
            0.0,
            window_duration_s,
            maximize=True,
            tol=1e-3,
        )
        alt_culm, az_culm, dist_culm = self._eval_all(diff_obj, dt_start, s_culm)

        # Find minimum slant range across window
        s_tca = self._golden_section_opt(
            lambda s: self._eval_range(diff_obj, dt_start, s),
            0.0,
            window_duration_s,
            maximize=False,
            tol=1e-3,
        )
        alt_tca, az_tca, dist_tca = self._eval_all(diff_obj, dt_start, s_tca)

        alt_start, az_start, dist_start = self._eval_all(diff_obj, dt_start, 0.0)
        alt_end, az_end, dist_end = self._eval_all(diff_obj, dt_start, window_duration_s)

        return SatellitePass(
            pass_id=1,
            aos=PassPoint(
                time=dt_start.isoformat(),
                azimuth_deg=round(az_start, 4),
                elevation_deg=round(alt_start, 4),
                slant_range_km=round(dist_start, 4),
            ),
            los=PassPoint(
                time=dt_end.isoformat(),
                azimuth_deg=round(az_end, 4),
                elevation_deg=round(alt_end, 4),
                slant_range_km=round(dist_end, 4),
            ),
            max_elevation=PassPoint(
                time=(dt_start + timedelta(seconds=s_culm)).isoformat(),
                azimuth_deg=round(az_culm, 4),
                elevation_deg=round(alt_culm, 4),
                slant_range_km=round(dist_culm, 4),
            ),
            closest_approach=ClosestApproachPoint(
                time=(dt_start + timedelta(seconds=s_tca)).isoformat(),
                slant_range_km=round(dist_tca, 4),
                elevation_deg=round(alt_tca, 4),
                azimuth_deg=round(az_tca, 4),
            ),
            duration_s=round(window_duration_s, 2),
            is_complete=False,
            is_continuous=True,
        )

    def _eval_alt(self, diff_obj: Any, dt_start: datetime, sec_offset: float) -> float:
        """Evaluate geometric elevation in degrees at a specific second offset."""
        dt = dt_start + timedelta(seconds=float(sec_offset))
        t = self.ts.from_datetime(dt)
        return float(diff_obj.at(t).altaz(temperature_C=None)[0].degrees)

    def _eval_range(self, diff_obj: Any, dt_start: datetime, sec_offset: float) -> float:
        """Evaluate slant range in kilometres at a specific second offset."""
        dt = dt_start + timedelta(seconds=float(sec_offset))
        t = self.ts.from_datetime(dt)
        return float(diff_obj.at(t).altaz(temperature_C=None)[2].km)

    def _eval_all(
        self, diff_obj: Any, dt_start: datetime, sec_offset: float
    ) -> tuple[float, float, float]:
        """Evaluate (elevation_deg, azimuth_deg, slant_range_km) at a specific second offset."""
        dt = dt_start + timedelta(seconds=float(sec_offset))
        t = self.ts.from_datetime(dt)
        alt, az, dist = diff_obj.at(t).altaz(temperature_C=None)
        return float(alt.degrees), float(az.degrees), float(dist.km)

    @staticmethod
    def _brent_root(
        f: Any, a: float, b: float, tol: float = 1e-4, max_iter: int = 30
    ) -> float:
        """Brent's method for finding root of f(x) = 0 in bracket [a, b]."""
        fa, fb = f(a), f(b)
        if fa * fb > 0:
            raise ValueError(f"Root is not bracketed in [{a}, {b}]: f({a})={fa}, f({b})={fb}")


        if abs(fa) < abs(fb):
            a, b = b, a
            fa, fb = fb, fa

        c, fc = a, fa
        mflag = True
        d = 0.0

        for _ in range(max_iter):
            if abs(fb) < tol or abs(b - a) < tol:
                return b

            if fa != fc and fb != fc:
                # Inverse quadratic interpolation
                s = (
                    (a * fb * fc) / ((fa - fb) * (fa - fc))
                    + (b * fa * fc) / ((fb - fa) * (fb - fc))
                    + (c * fa * fb) / ((fc - fa) * (fc - fb))
                )
            else:
                # Secant method
                s = b - fb * (b - a) / (fb - fa)

            cond1 = not ((3 * a + b) / 4 < s < b if a < b else b < s < (3 * a + b) / 4)
            cond2 = mflag and abs(s - b) >= abs(b - c) / 2
            cond3 = not mflag and abs(s - b) >= abs(c - d) / 2
            cond4 = mflag and abs(b - c) < tol
            cond5 = not mflag and abs(c - d) < tol

            if cond1 or cond2 or cond3 or cond4 or cond5:
                s = (a + b) / 2
                mflag = True
            else:
                mflag = False

            fs = f(s)
            d = c
            c, fc = b, fb

            if fa * fs < 0:
                b, fb = s, fs
            else:
                a, fa = s, fs

            if abs(fa) < abs(fb):
                a, b = b, a
                fa, fb = fb, fa

        return b

    @staticmethod
    def _golden_section_opt(
        f: Any,
        a: float,
        b: float,
        maximize: bool = True,
        tol: float = 1e-3,
        max_iter: int = 30,
    ) -> float:
        """Golden Section search for 1D extremum in [a, b]."""
        invphi = (math.sqrt(5.0) - 1.0) / 2.0  # ~0.6180339887
        invphi2 = (3.0 - math.sqrt(5.0)) / 2.0  # ~0.3819660113

        h = b - a
        if h <= tol:
            return (a + b) / 2.0

        c = a + invphi2 * h
        d = a + invphi * h
        yc = f(c)
        yd = f(d)

        for _ in range(max_iter):
            if abs(b - a) <= tol:
                break

            better = (yc > yd) if maximize else (yc < yd)
            if better:
                b = d
                d = c
                yd = yc
                h = invphi * h
                c = a + invphi2 * h
                yc = f(c)
            else:
                a = c
                c = d
                yc = yd
                h = invphi * h
                d = a + invphi * h
                yd = f(d)

        return (a + b) / 2.0

    def _parse_target_time(self, target_time: Union[datetime, str]) -> datetime:
        """Parse and normalize timestamp to UTC datetime."""
        if isinstance(target_time, str):
            cleaned = target_time.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(cleaned)
            except Exception as exc:
                raise PassPredictionError(
                    f"Invalid ISO 8601 timestamp string '{target_time}': {exc}"
                ) from exc
        elif isinstance(target_time, datetime):
            dt = target_time
        else:
            raise PassPredictionError(
                f"Target time must be datetime or ISO 8601 string, got {type(target_time)}"
            )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt
