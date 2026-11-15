import logging
from typing import Optional

from app.config import CELESTRAK_CACHE_TTL_SECONDS
from app.orbit.cache import TTLCache
from app.orbit.celestrak import CelesTrakClient
from app.orbit.exceptions import (
    RecordValidationError,
    SatelliteNotFoundError,
)
from app.orbit.models import OrbitalRecord

logger = logging.getLogger(__name__)


class OrbitService:
    """Business logic for querying, validating, and caching satellite orbital data."""

    def __init__(
        self,
        client: Optional[CelesTrakClient] = None,
        cache: Optional[TTLCache] = None,
    ):
        self.client = client if client is not None else CelesTrakClient()
        self.cache = (
            cache
            if cache is not None
            else TTLCache(default_ttl_seconds=CELESTRAK_CACHE_TTL_SECONDS)
        )


    async def get_satellite_by_catnr(
        self, catnr: int, use_cache: bool = True
    ) -> Optional[OrbitalRecord]:
        """Retrieve and validate orbital element data for a satellite by its NORAD catalog number."""
        cache_key = f"catnr:{catnr}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        raw_records = await self.client.fetch_gp_by_catnr(catnr)
        if not raw_records:
            return None

        # Parse first record
        try:
            record = OrbitalRecord.model_validate(raw_records[0])
        except Exception as exc:
            raise RecordValidationError(
                f"Failed to validate orbital record for NORAD ID {catnr}: {exc}"
            ) from exc

        if use_cache:
            self.cache.set(cache_key, record)

        return record

    async def get_satellites_by_group(
        self, group: str, use_cache: bool = True
    ) -> list[OrbitalRecord]:
        """Retrieve and validate orbital elements for a named satellite group."""
        group_norm = group.strip().lower()
        cache_key = f"group:{group_norm}"

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        raw_records = await self.client.fetch_gp_by_group(group)
        if not raw_records:
            return []

        valid_records: list[OrbitalRecord] = []
        for raw in raw_records:
            try:
                rec = OrbitalRecord.model_validate(raw)
                valid_records.append(rec)
                # Populate individual catnr cache as well to save future requests
                if use_cache:
                    self.cache.set(f"catnr:{rec.norad_cat_id}", rec)
            except Exception as exc:
                cat_id = raw.get("NORAD_CAT_ID", "unknown")
                logger.warning(
                    "Skipping invalid orbital record (NORAD ID %s): %s", cat_id, exc
                )

        if use_cache:
            self.cache.set(cache_key, valid_records)

        return valid_records
