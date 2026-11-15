from typing import Any
from fastapi import APIRouter, HTTPException, Query, status

from app.orbit.exceptions import (
    CelesTrakError,
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
    catnr: int,
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
    group: str,
    no_cache: bool = Query(False, description="Bypass cache and force refresh"),
) -> Any:
    try:
        records = await orbit_service.get_satellites_by_group(group, use_cache=not no_cache)
        return records
    except CelesTrakTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except (CelesTrakError, RecordValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )
