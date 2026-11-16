from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Path, Query, status

from app.orbit.exceptions import (
    CelesTrakError,
    CelesTrakRateLimitError,
    CelesTrakTimeoutError,
    DatabaseError,
    ObserverError,
    PassPredictionError,
    PropagationError,
    RecordValidationError,
)
from app.orbit.models import (
    ObserverLocation,
    OrbitalRecord,
    PassPredictionResult,
    PropagationResult,
    StoredOrbitalRecord,
    TopocentricResult,
)
from app.orbit.observer import ObserverService
from app.orbit.pass_predictor import PassPredictionService
from app.orbit.propagator import OrbitPropagator
from app.orbit.service import OrbitService

router = APIRouter(prefix="/api/orbit", tags=["Orbit Engine"])
orbit_service = OrbitService()
orbit_propagator = OrbitPropagator()
observer_service = ObserverService()
pass_prediction_service = PassPredictionService(observer_service=observer_service)




@router.get(
    "/satellite/{catnr}",
    response_model=OrbitalRecord,
    summary="Get orbital element data for a satellite by NORAD Catalog Number",
)
async def get_satellite_by_catnr(
    catnr: int = Path(
        ...,
        ge=1,
        le=999999999,
        description="NORAD Catalog Number (1 to 9 digits)",
        examples=[25544],
    ),
    no_cache: bool = Query(False, description="Bypass cache and force refresh"),
) -> Any:
    try:
        record = await orbit_service.get_satellite_by_catnr(catnr, use_cache=not no_cache)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Satellite with NORAD ID {catnr} not found",
            )
        return record
    except HTTPException:
        raise
    except CelesTrakRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        )


@router.get(
    "/stored/{catnr}",
    response_model=StoredOrbitalRecord,
    summary="Get persistent stored orbital record for a satellite by NORAD Catalog Number",
)
async def get_stored_satellite_by_catnr(
    catnr: int = Path(
        ...,
        ge=1,
        le=999999999,
        description="NORAD Catalog Number (1 to 9 digits)",
        examples=[25544],
    ),
) -> Any:
    try:
        record = await orbit_service.get_stored_satellite(catnr)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No stored orbital record for NORAD ID {catnr}",
            )
        return record
    except HTTPException:
        raise
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        )


@router.get(
    "/stored",
    response_model=list[StoredOrbitalRecord],
    summary="List stored orbital records in persistent database",
)
async def list_stored_satellites(
    limit: int = Query(100, ge=1, le=1000, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> Any:
    try:
        return await orbit_service.list_stored_satellites(limit=limit, offset=offset)
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        )


@router.get(
    "/satellite/{catnr}/propagate",
    response_model=PropagationResult,
    summary="Propagate satellite position and velocity to a specified UTC timestamp using SGP4",
)
async def propagate_satellite_at_time(
    catnr: int = Path(
        ...,
        ge=1,
        le=999999999,
        description="NORAD Catalog Number (1 to 9 digits)",
        examples=[25544],
    ),
    time: Optional[str] = Query(
        None,
        description="Target UTC timestamp in ISO 8601 format (defaults to current UTC time)",
        examples=["2026-11-15T05:00:00Z"],
    ),
    no_cache: bool = Query(False, description="Bypass cache for orbital elements"),
) -> Any:
    try:
        record = await orbit_service.get_satellite_by_catnr(catnr, use_cache=not no_cache)
    except CelesTrakRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite with NORAD ID {catnr} not found",
        )

    target_time = time or datetime.now(timezone.utc).isoformat()
    try:
        return orbit_propagator.propagate(record, target_time)
    except PropagationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )


@router.get(
    "/satellite/{catnr}/observe",
    response_model=TopocentricResult,
    summary="Calculate topocentric coordinates (azimuth, elevation, slant range) from an observer location",
)
async def observe_satellite_at_time(
    catnr: int = Path(
        ...,
        ge=1,
        le=999999999,
        description="NORAD Catalog Number (1 to 9 digits)",
        examples=[25544],
    ),
    lat: float = Query(
        ...,
        ge=-90.0,
        le=90.0,
        description="Observer latitude in degrees [-90.0, +90.0]",
        examples=[28.6139],
    ),
    lon: float = Query(
        ...,
        ge=-180.0,
        le=180.0,
        description="Observer longitude in degrees [-180.0, +180.0]",
        examples=[77.2090],
    ),
    alt_m: float = Query(
        0.0,
        ge=-500.0,
        le=9000.0,
        description="Observer height above WGS84 ellipsoid in metres (default 0.0)",
        examples=[216.0],
    ),
    time: Optional[str] = Query(
        None,
        description="Target UTC timestamp in ISO 8601 format (defaults to current UTC time)",
        examples=["2026-11-15T05:00:00Z"],
    ),
    no_cache: bool = Query(False, description="Bypass cache for orbital elements"),
) -> Any:
    try:
        record = await orbit_service.get_satellite_by_catnr(catnr, use_cache=not no_cache)
    except CelesTrakRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite with NORAD ID {catnr} not found",
        )

    target_time = time or datetime.now(timezone.utc).isoformat()
    try:
        observer = ObserverLocation(latitude=lat, longitude=lon, elevation_m=alt_m)
        return observer_service.observe(record, observer, target_time)
    except (PropagationError, ObserverError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )


@router.get(
    "/satellite/{catnr}/passes",
    response_model=PassPredictionResult,
    summary="Predict visible satellite passes above a minimum elevation threshold for a ground observer",
)
async def predict_satellite_passes(
    catnr: int = Path(
        ...,
        ge=1,
        le=999999999,
        description="NORAD Catalog Number (1 to 9 digits)",
        examples=[25544],
    ),
    lat: float = Query(
        ...,
        ge=-90.0,
        le=90.0,
        description="Observer geodetic latitude in degrees [-90.0, +90.0]",
        examples=[28.6139],
    ),
    lon: float = Query(
        ...,
        ge=-180.0,
        le=180.0,
        description="Observer geodetic longitude in degrees [-180.0, +180.0]",
        examples=[77.2090],
    ),
    alt_m: float = Query(
        0.0,
        ge=-500.0,
        le=9000.0,
        description="Observer height above WGS84 ellipsoid in metres (default 0.0)",
        examples=[216.0],
    ),
    start_time: Optional[str] = Query(
        None,
        description="Start of observation window in ISO 8601 UTC format (defaults to current UTC time)",
        examples=["2026-11-15T00:00:00Z"],
    ),
    end_time: Optional[str] = Query(
        None,
        description="End of observation window in ISO 8601 UTC format (defaults to start_time + days)",
        examples=["2026-11-16T00:00:00Z"],
    ),
    days: Optional[float] = Query(
        None,
        ge=0.01,
        le=14.0,
        description="Window duration in days (used if end_time is omitted, defaults to 1.0 day)",
        examples=[1.0],
    ),
    min_elevation: float = Query(
        10.0,
        ge=0.0,
        le=85.0,
        description="Minimum geometric elevation threshold in degrees (default 10.0)",
        examples=[10.0],
    ),
    no_cache: bool = Query(False, description="Bypass cache for orbital elements"),
) -> Any:
    try:
        record = await orbit_service.get_satellite_by_catnr(catnr, use_cache=not no_cache)
    except CelesTrakRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite with NORAD ID {catnr} not found",
        )

    # Determine start and end times
    start_dt_str = start_time or datetime.now(timezone.utc).isoformat()
    if end_time is not None:
        end_dt_str = end_time
    else:
        window_days = days if days is not None else 1.0
        try:
            cleaned_start = start_dt_str.strip()
            if cleaned_start.endswith("Z"):
                cleaned_start = cleaned_start[:-1] + "+00:00"
            dt_parsed = datetime.fromisoformat(cleaned_start)
            if dt_parsed.tzinfo is None:
                dt_parsed = dt_parsed.replace(tzinfo=timezone.utc)
            end_dt_str = (dt_parsed + timedelta(days=window_days)).isoformat()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid start_time format: {exc}",
            )

    try:
        observer = ObserverLocation(latitude=lat, longitude=lon, elevation_m=alt_m)
        return pass_prediction_service.predict_passes(
            record=record,
            observer=observer,
            start_time=start_dt_str,
            end_time=end_dt_str,
            min_elevation_deg=min_elevation,
        )
    except (PassPredictionError, PropagationError, ObserverError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )




@router.get(
    "/group/{group}",
    response_model=list[OrbitalRecord],
    summary="Get orbital element data for a satellite group (e.g. 'stations', 'visual')",
)
async def get_satellites_by_group(
    group: str = Path(
        ...,
        min_length=1,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="Satellite group name (e.g. 'stations', 'visual', 'weather')",
        examples=["stations"],
    ),
    no_cache: bool = Query(False, description="Bypass cache and force refresh"),
) -> Any:
    try:
        records = await orbit_service.get_satellites_by_group(group, use_cache=not no_cache)
        return records
    except CelesTrakRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )
