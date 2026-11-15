from datetime import datetime, timezone
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Path, Query, status

from app.orbit.exceptions import (
    CelesTrakError,
    CelesTrakRateLimitError,
    CelesTrakTimeoutError,
    PropagationError,
    RecordValidationError,
)
from app.orbit.models import OrbitalRecord, PropagationResult
from app.orbit.propagator import OrbitPropagator
from app.orbit.service import OrbitService

router = APIRouter(prefix="/api/orbit", tags=["Orbit Engine"])
orbit_service = OrbitService()
orbit_propagator = OrbitPropagator()


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
