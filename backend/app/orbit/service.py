import logging
from typing import Optional

from app.config import CELESTRAK_CACHE_TTL_SECONDS
from app.orbit.cache import TTLCache
from app.orbit.celestrak import CelesTrakClient
from app.orbit.exceptions import (
    CelesTrakError,
    DatabaseError,
    RecordValidationError,
    SatelliteNotFoundError,
)
from app.orbit.models import OrbitalRecord, StoredOrbitalRecord
from app.orbit.storage import OrbitStorage

logger = logging.getLogger(__name__)


class OrbitService:
    """Business logic for querying, validating, caching, and persisting satellite orbital data."""

    def __init__(
        self,
        client: Optional[CelesTrakClient] = None,
        cache: Optional[TTLCache] = None,
        storage: Optional[OrbitStorage] = None,
        strict_storage: bool = False,
    ):
        self.client = client if client is not None else CelesTrakClient()
        self.cache = (
            cache
            if cache is not None
            else TTLCache(default_ttl_seconds=CELESTRAK_CACHE_TTL_SECONDS)
        )
        self.storage = storage if storage is not None else OrbitStorage()
        self.strict_storage = strict_storage

    async def get_satellite_by_catnr(
        self,
        catnr: int,
        use_cache: bool = True,
        fallback_to_storage: bool = False,
    ) -> Optional[OrbitalRecord]:
        """Retrieve and validate orbital element data for a satellite by its NORAD catalog number."""
        cache_key = f"catnr:{catnr}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            raw_records = await self.client.fetch_gp_by_catnr(catnr)
        except CelesTrakError as exc:
            if fallback_to_storage and self.storage is not None:
                stored = self.storage.get_record(catnr)
                if stored is not None:
                    logger.warning(
                        "Upstream fetch failed for NORAD ID %s; falling back to stored record: %s",
                        catnr,
                        exc,
                    )
                    if use_cache:
                        self.cache.set(cache_key, stored)
                    return stored
            raise

        if not raw_records:
            if fallback_to_storage and self.storage is not None:
                stored = self.storage.get_record(catnr)
                if stored is not None:
                    return stored
            return None

        # Parse first record
        try:
            record = OrbitalRecord.model_validate(raw_records[0])
        except Exception as exc:
            raise RecordValidationError(
                f"Failed to validate orbital record for NORAD ID {catnr}: {exc}"
            ) from exc

        # Persist to storage
        if self.storage is not None:
            try:
                self.storage.upsert(record, source="celestrak")
            except DatabaseError as exc:
                if self.strict_storage:
                    raise
                logger.error(
                    "Failed to persist orbital record for NORAD ID %s: %s", catnr, exc
                )

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

        # Batch persist valid records to storage
        if self.storage is not None and valid_records:
            try:
                self.storage.upsert_many(valid_records, source="celestrak")
            except DatabaseError as exc:
                if self.strict_storage:
                    raise
                logger.error(
                    "Failed to persist group '%s' records to storage: %s", group, exc
                )

        if use_cache:
            self.cache.set(cache_key, valid_records)

        return valid_records

    async def get_stored_satellite(self, catnr: int) -> Optional[StoredOrbitalRecord]:
        """Retrieve a stored orbital record directly from persistent storage."""
        if self.storage is None:
            return None
        return self.storage.get(catnr)

    async def list_stored_satellites(
        self, limit: int = 100, offset: int = 0
    ) -> list[StoredOrbitalRecord]:
        """List stored orbital records from persistent storage."""
        if self.storage is None:
            return []
        return self.storage.list_records(limit=limit, offset=offset)

    async def count_stored_satellites(self) -> int:
        """Count total orbital records in persistent storage."""
        if self.storage is None:
            return 0
        return self.storage.count()
