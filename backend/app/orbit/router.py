from typing import Any
from fastapi import APIRouter, HTTPException, Path, Query, status

from app.orbit.exceptions import (
    CelesTrakError,
    CelesTrakRateLimitError,
    CelesTrakTimeoutError,
    RecordValidationError,
)
from app.orbit.models import OrbitalRecord
from app.orbit.service import OrbitService

router = APIRouter(prefix="/api/orbit", tags=["Orbit Engine"])
orbit_service = OrbitService()


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
