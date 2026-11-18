"""Historical orbital record selection service.

Implements conservative causal selection policy for historical satellite element sets,
providing deterministic tie-breaking, regime-aware operational thresholds,
candidate contextualization, and quality classification.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Optional

from app.orbit.exceptions import RecordValidationError
from app.orbit.models import (
    CandidateContext,
    HistoricalOrbitalRecord,
    HistoricalSelectionRequest,
    HistoricalSelectionResult,
    OrbitalRecord,
    OrbitalRegimeType,
    SelectionMetadata,
    SelectionPolicyConfig,
)
from app.orbit.storage import OrbitStorage, parse_epoch_timestamp

logger = logging.getLogger(__name__)


def classify_orbital_regime(record: OrbitalRecord) -> str:
    """Classify satellite orbital regime from its mean motion and eccentricity.

    Operational classification rules:
    - LEO (Low Earth Orbit): mean_motion > 11.25 rev/day (period < ~128 min, altitude < ~2,000 km)
    - GEO (Geostationary/Geosynchronous): 0.95 <= mean_motion <= 1.05 rev/day and eccentricity < 0.2
    - MEO (Medium Earth Orbit): 1.05 < mean_motion <= 11.25 rev/day and eccentricity < 0.25 (e.g. GPS, Galileo, Glonass)
    - OTHER: all other configurations (e.g. highly elliptical orbits such as Molniya/GTO, high-eccentricity
      geosynchronous orbits like Tundra, deep space, sub-orbital, unclassified)

    Note:
        This heuristic is an operational classification designed specifically to select appropriate
        staleness thresholds; it is not claimed to be a complete or rigorous astrodynamical taxonomy.
        High-eccentricity objects in the MEO band (such as Molniya orbits with e ~ 0.74) are routed
        to OTHER to avoid applying overly permissive circular MEO thresholds (e.g. 7-day nominal)
        to orbits experiencing significant perigee atmospheric drag.
    """
    mm = record.mean_motion
    ecc = record.eccentricity

    if mm > 11.25:
        return "LEO"
    if 0.95 <= mm <= 1.05 and ecc < 0.2:
        return "GEO"
    if 1.05 < mm <= 11.25 and ecc < 0.25:
        return "MEO"
    return "OTHER"


class HistoricalSelectionService:
    """Service for selecting the optimal historical orbital element set for a requested UTC timestamp.

    Selection Policy:
        The initial conservative policy prefers an orbital element set whose epoch is at
        or before the requested timestamp (causal prior record) to avoid backwards propagation
        from future elements. Subsequent candidates and their time differences are retained
        in metadata as selection context.

        When multiple element sets share the exact same closest epoch, deterministic tie-breaking
        prefers the record with the latest upstream creation/publication date.

        Note: This policy is an operational heuristic designed for causality and traceability;
        it is not described as scientifically optimal or universally most accurate for every
        propagation scenario.
    """

    def __init__(
        self,
        storage: OrbitStorage,
        config: Optional[SelectionPolicyConfig] = None,
    ):
        """Initialize historical selection service with persistent storage and policy config."""
        self.storage = storage
        self.config = config or SelectionPolicyConfig()

    def select(self, request: HistoricalSelectionRequest) -> HistoricalSelectionResult:
        """Select a historical orbital record matching the request specifications."""
        # 1. Parse and normalize requested timestamp to UTC
        try:
            requested_ts = parse_epoch_timestamp(request.timestamp)
        except Exception as exc:
            raise RecordValidationError(
                f"Invalid requested timestamp '{request.timestamp}': {exc}"
            ) from exc

        dt = datetime.fromtimestamp(requested_ts, tz=timezone.utc)
        if dt.microsecond > 0:
            norm_requested_time = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            norm_requested_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 2. Query bounded prior and subsequent candidates using SQLite covering index
        prior_candidates = self.storage.find_prior_history_candidates(
            norad_cat_id=request.norad_cat_id,
            target_timestamp=requested_ts,
            limit=50,
        )
        subsequent_candidates = self.storage.find_subsequent_history_candidates(
            norad_cat_id=request.norad_cat_id,
            target_timestamp=requested_ts,
            limit=5,
        )

        # 3. Contextualize closest subsequent candidate if one exists
        subsequent_context: Optional[CandidateContext] = None
        if subsequent_candidates:
            sub_rec = subsequent_candidates[0]
            time_diff_sub = requested_ts - sub_rec.epoch_timestamp  # Negative: sub_epoch > requested_ts
            abs_dist_sub_hours = abs(time_diff_sub) / 3600.0
            subsequent_context = CandidateContext(
                record_id=sub_rec.id,
                gp_id=sub_rec.gp_id,
                epoch=sub_rec.epoch,
                epoch_timestamp=sub_rec.epoch_timestamp,
                creation_date=sub_rec.creation_date,
                creation_date_timestamp=sub_rec.creation_date_timestamp,
                source=sub_rec.source,
                time_difference_seconds=time_diff_sub,
                absolute_distance_hours=abs_dist_sub_hours,
                is_prior=False,
            )

        # 4. Handle Case A: No prior candidates exist
        if not prior_candidates:
            regime = request.orbital_regime.upper() if request.orbital_regime else (
                classify_orbital_regime(subsequent_candidates[0]) if subsequent_candidates else "UNKNOWN"
            )
            th = self._get_thresholds_for_regime(regime)
            nominal_hours = request.nominal_threshold_hours if request.nominal_threshold_hours is not None else th.nominal_hours
            max_hours = request.max_epoch_distance_hours if request.max_epoch_distance_hours is not None else th.max_hours

            metadata = SelectionMetadata(
                policy_name=self.config.policy_name,
                orbital_regime=regime,
                nominal_threshold_hours=nominal_hours,
                max_threshold_hours=max_hours,
                allow_unreliable_requested=request.allow_unreliable,
                candidates_evaluated=len(subsequent_candidates),
                subsequent_candidate=subsequent_context,
                alternative_prior_candidates_count=0,
            )

            if subsequent_context is not None:
                reason = (
                    f"No prior historical orbital records found with epoch prior to or matching "
                    f"requested timestamp {norm_requested_time}. Found closest subsequent record "
                    f"at {subsequent_context.epoch} ({subsequent_context.absolute_distance_hours:.2f}h "
                    f"after requested time)."
                )
            else:
                reason = f"No historical orbital records found for NORAD ID {request.norad_cat_id}."

            return HistoricalSelectionResult(
                norad_cat_id=request.norad_cat_id,
                requested_time=norm_requested_time,
                requested_timestamp=requested_ts,
                quality="unavailable",
                reason=reason,
                record=None,
                record_id=None,
                gp_id=None,
                element_epoch=None,
                creation_date=None,
                source=None,
                time_difference_seconds=None,
                absolute_distance_hours=None,
                is_prior=None,
                metadata=metadata,
            )

        # 5. Handle Case B: Prior candidate(s) exist
        # Group candidates sharing the exact closest prior epoch
        closest_epoch_ts = prior_candidates[0].epoch_timestamp
        tied_candidates = [
            c for c in prior_candidates if c.epoch_timestamp == closest_epoch_ts
        ]
        alternative_priors_count = len(prior_candidates) - len(tied_candidates)

        # Deterministic tie-breaking among records sharing the exact same closest epoch:
        # 1. Prefer record with creation_date over missing creation_date
        # 2. Prefer latest available creation_date_timestamp
        # 3. Deterministic fallback on highest gp_id, then highest id
        def candidate_tie_breaker(rec: HistoricalOrbitalRecord) -> tuple[int, float, int, int]:
            has_cd = 1 if rec.creation_date_timestamp is not None else 0
            cd_ts = rec.creation_date_timestamp if rec.creation_date_timestamp is not None else -1.0
            gp = rec.gp_id if rec.gp_id is not None else -1
            rec_id = rec.id if rec.id is not None else -1
            return (has_cd, cd_ts, gp, rec_id)

        best_record = max(tied_candidates, key=candidate_tie_breaker)

        # 6. Determine orbital regime & applicable thresholds
        if request.orbital_regime:
            regime = request.orbital_regime.upper()
        else:
            regime = classify_orbital_regime(best_record)

        th = self._get_thresholds_for_regime(regime)
        nominal_hours = request.nominal_threshold_hours if request.nominal_threshold_hours is not None else th.nominal_hours
        max_hours = request.max_epoch_distance_hours if request.max_epoch_distance_hours is not None else th.max_hours

        # 7. Compute temporal metrics
        time_diff_sec = requested_ts - best_record.epoch_timestamp  # Non-negative for prior records
        abs_dist_hours = time_diff_sec / 3600.0

        # 8. Classify quality based on regime thresholds and allow_unreliable flag
        if abs_dist_hours <= nominal_hours:
            quality = "nominal"
            reason = (
                f"Selected closest prior record at {best_record.epoch} within nominal threshold "
                f"for {regime} ({abs_dist_hours:.2f}h <= {nominal_hours:.2f}h)."
            )
            selected_record_out: Optional[HistoricalOrbitalRecord] = best_record
        elif abs_dist_hours <= max_hours:
            quality = "degraded"
            reason = (
                f"Selected closest prior record at {best_record.epoch} exceeds nominal threshold "
                f"but is within operational maximum for {regime} ({abs_dist_hours:.2f}h <= {max_hours:.2f}h)."
            )
            selected_record_out = best_record
        else:
            # Stale record beyond maximum threshold
            if request.allow_unreliable:
                quality = "unreliable"
                reason = (
                    f"Closest prior record at {best_record.epoch} exceeds operational maximum "
                    f"for {regime} ({abs_dist_hours:.2f}h > {max_hours:.2f}h). "
                    f"Returned with quality='unreliable' because allow_unreliable=True."
                )
                selected_record_out = best_record
            else:
                quality = "unavailable"
                reason = (
                    f"Closest prior record at {best_record.epoch} exceeds operational maximum "
                    f"for {regime} ({abs_dist_hours:.2f}h > {max_hours:.2f}h) and allow_unreliable=False."
                )
                selected_record_out = None

        metadata = SelectionMetadata(
            policy_name=self.config.policy_name,
            orbital_regime=regime,
            nominal_threshold_hours=nominal_hours,
            max_threshold_hours=max_hours,
            allow_unreliable_requested=request.allow_unreliable,
            candidates_evaluated=len(prior_candidates) + len(subsequent_candidates),
            subsequent_candidate=subsequent_context,
            alternative_prior_candidates_count=alternative_priors_count,
        )

        return HistoricalSelectionResult(
            norad_cat_id=request.norad_cat_id,
            requested_time=norm_requested_time,
            requested_timestamp=requested_ts,
            quality=quality,
            reason=reason,
            record=selected_record_out,
            record_id=selected_record_out.id if selected_record_out is not None else None,
            gp_id=selected_record_out.gp_id if selected_record_out is not None else None,
            element_epoch=selected_record_out.epoch if selected_record_out is not None else None,
            creation_date=selected_record_out.creation_date if selected_record_out is not None else None,
            source=selected_record_out.source if selected_record_out is not None else None,
            time_difference_seconds=time_diff_sec if selected_record_out is not None else None,
            absolute_distance_hours=abs_dist_hours if selected_record_out is not None else None,
            is_prior=True if selected_record_out is not None else None,
            metadata=metadata,
        )

    def select_record(
        self,
        norad_cat_id: int,
        timestamp: str | datetime,
        allow_unreliable: bool = False,
        max_epoch_distance_hours: Optional[float] = None,
        nominal_threshold_hours: Optional[float] = None,
        orbital_regime: Optional[OrbitalRegimeType] = None,
    ) -> HistoricalSelectionResult:
        """Convenience method accepting raw arguments for historical orbital record selection."""
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                ts_str = timestamp.replace(tzinfo=timezone.utc).isoformat()
            else:
                ts_str = timestamp.astimezone(timezone.utc).isoformat()
        else:
            ts_str = str(timestamp)

        req = HistoricalSelectionRequest(
            norad_cat_id=norad_cat_id,
            timestamp=ts_str,
            allow_unreliable=allow_unreliable,
            max_epoch_distance_hours=max_epoch_distance_hours,
            nominal_threshold_hours=nominal_threshold_hours,
            orbital_regime=orbital_regime,
        )
        return self.select(req)

    def _get_thresholds_for_regime(self, regime: str):
        """Map orbital regime identifier to applicable operational thresholds.

        Regimes classified as OTHER or unclassified map to default_thresholds
        (24-hour nominal / 72-hour maximum). This represents an operational conservative
        fallback, not a scientific or astrodynamical guarantee.
        """
        if regime == "LEO":
            return self.config.leo_thresholds
        if regime == "GEO":
            return self.config.geo_thresholds
        if regime == "MEO":
            return self.config.meo_thresholds
        return self.config.default_thresholds
