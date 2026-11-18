from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Vector3D(BaseModel):
    """3D Cartesian vector in space."""

    x: float = Field(..., description="X component in km or km/s")
    y: float = Field(..., description="Y component in km or km/s")
    z: float = Field(..., description="Z component in km or km/s")


class PropagationResult(BaseModel):
    """SGP4 satellite orbital state vector at a specific UTC timestamp."""

    norad_cat_id: int = Field(..., description="NORAD Catalog Number")
    object_name: str = Field(..., description="Satellite name")
    timestamp: str = Field(..., description="UTC propagation timestamp in ISO 8601 format")
    coordinate_frame: str = Field(
        default="GCRS",
        description="Geocentric Celestial Reference System (ICRS-aligned geocentric frame)",
    )
    position_km: Vector3D = Field(
        ..., description="Geocentric position vector in kilometres [X, Y, Z]"
    )
    velocity_km_per_s: Vector3D = Field(
        ..., description="Velocity vector in kilometres per second [VX, VY, VZ]"
    )


class ObserverLocation(BaseModel):
    """Geodetic location of a ground observer referenced to the WGS84 ellipsoid."""

    latitude: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Observer geodetic latitude in degrees [-90.0, +90.0]",
        examples=[28.6139],
    )
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Observer geodetic longitude in degrees [-180.0, +180.0]",
        examples=[77.2090],
    )
    elevation_m: float = Field(
        default=0.0,
        ge=-500.0,
        le=9000.0,
        description="Observer height above WGS84 ellipsoid in metres (default 0.0)",
        examples=[216.0],
    )


class TopocentricResult(BaseModel):
    """Topocentric coordinates of a satellite relative to a ground observer."""

    norad_cat_id: int = Field(..., description="NORAD Catalog Number")
    object_name: str = Field(..., description="Satellite name")
    timestamp: str = Field(..., description="UTC observation timestamp in ISO 8601 format")
    observer: ObserverLocation = Field(..., description="Observer location on WGS84 ellipsoid")
    azimuth_deg: float = Field(
        ...,
        ge=0.0,
        le=360.0,
        description="Topocentric azimuth in degrees clockwise from True North [0, 360)",
    )
    elevation_deg: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Topocentric geometric elevation in degrees above local horizon [-90, +90]",
    )
    slant_range_km: float = Field(
        ..., ge=0.0, description="Direct distance (slant range) from observer to satellite in km"
    )
    range_rate_km_per_s: Optional[float] = Field(
        default=None,
        description="Slant range rate (radial velocity) in km/s (negative = approaching, positive = receding)",
    )
    relative_position_km: Vector3D = Field(
        ..., description="Observer-to-satellite relative position vector in GCRS [X, Y, Z] in km"
    )
    relative_velocity_km_per_s: Vector3D = Field(
        ..., description="Observer-to-satellite relative velocity vector in GCRS [VX, VY, VZ] in km/s"
    )


class PassPoint(BaseModel):
    """Specific event point in a satellite pass (AOS, LOS, or Culmination)."""

    time: str = Field(..., description="UTC timestamp in ISO 8601 format")
    azimuth_deg: float = Field(
        ...,
        ge=0.0,
        le=360.0,
        description="Topocentric azimuth in degrees clockwise from True North [0, 360)",
    )
    elevation_deg: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Topocentric geometric elevation in degrees above local horizon [-90, +90]",
    )
    slant_range_km: float = Field(
        ..., ge=0.0, description="Direct distance from observer to satellite in km"
    )


class ClosestApproachPoint(BaseModel):
    """Time of Closest Approach (TCA) / minimum slant range point in a pass."""

    time: str = Field(..., description="UTC timestamp in ISO 8601 format")
    slant_range_km: float = Field(
        ..., ge=0.0, description="Minimum slant range from observer in km"
    )
    elevation_deg: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Topocentric geometric elevation at minimum range in degrees",
    )
    azimuth_deg: float = Field(
        ...,
        ge=0.0,
        le=360.0,
        description="Topocentric azimuth at minimum range in degrees",
    )


class SatellitePass(BaseModel):
    """Predicted visibility window of a satellite passing above an elevation threshold."""

    pass_id: int = Field(..., ge=1, description="Sequential index of pass in requested window (1-indexed)")
    aos: PassPoint = Field(..., description="Acquisition of Signal (rise above elevation threshold)")
    los: PassPoint = Field(..., description="Loss of Signal (fall below elevation threshold)")
    max_elevation: PassPoint = Field(..., description="Culmination / point of maximum geometric elevation")
    closest_approach: ClosestApproachPoint = Field(
        ..., description="Time of Closest Approach (minimum slant range)"
    )
    duration_s: float = Field(..., ge=0.0, description="Pass duration in seconds (between AOS and LOS)")
    is_complete: bool = Field(
        ...,
        description="True if both AOS and LOS occurred within requested window; False if truncated by boundaries",
    )
    is_continuous: bool = Field(
        default=False,
        description="True if satellite remained continuously above threshold without rising/setting (e.g. GEO)",
    )


class PassPredictionResult(BaseModel):
    """Collection of predicted passes for a satellite within a specified observation window."""

    norad_cat_id: int = Field(..., description="NORAD Catalog Number")
    object_name: str = Field(..., description="Satellite name")
    observer: ObserverLocation = Field(..., description="Observer location on WGS84 ellipsoid")
    start_time: str = Field(..., description="Start of observation window in ISO 8601 UTC format")
    end_time: str = Field(..., description="End of observation window in ISO 8601 UTC format")
    min_elevation_deg: float = Field(..., description="Configured minimum elevation threshold in degrees")
    pass_count: int = Field(..., ge=0, description="Number of passes found in the requested window")
    passes: list[SatellitePass] = Field(default_factory=list, description="List of predicted satellite passes")




class OrbitalRecord(BaseModel):
    """Orbital element set following CCSDS OMM / CelesTrak GP JSON format."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    object_name: str = Field(alias="OBJECT_NAME", description="Satellite name")
    object_id: str = Field(alias="OBJECT_ID", description="International designator (COSPAR ID)")
    epoch: str = Field(alias="EPOCH", description="UTC epoch timestamp (ISO 8601)")
    mean_motion: float = Field(alias="MEAN_MOTION", description="Mean motion in revolutions per day")
    eccentricity: float = Field(alias="ECCENTRICITY", description="Orbital eccentricity")
    inclination: float = Field(alias="INCLINATION", description="Orbital inclination in degrees")
    ra_of_asc_node: float = Field(alias="RA_OF_ASC_NODE", description="Right Ascension of Ascending Node in degrees")
    arg_of_pericenter: float = Field(alias="ARG_OF_PERICENTER", description="Argument of Pericenter in degrees")
    mean_anomaly: float = Field(alias="MEAN_ANOMALY", description="Mean Anomaly in degrees")
    norad_cat_id: int = Field(alias="NORAD_CAT_ID", description="NORAD Catalog Number")

    # Optional / auxiliary fields in standard GP element sets (with null-safe fallbacks)
    ephemeris_type: Optional[int] = Field(default=0, alias="EPHEMERIS_TYPE")
    classification_type: Optional[str] = Field(default="U", alias="CLASSIFICATION_TYPE")
    element_set_no: Optional[int] = Field(default=None, alias="ELEMENT_SET_NO")
    rev_at_epoch: Optional[int] = Field(default=None, alias="REV_AT_EPOCH")
    bstar: Optional[float] = Field(default=0.0, alias="BSTAR", description="SGP4 drag term (1/earth radii)")
    mean_motion_dot: Optional[float] = Field(default=0.0, alias="MEAN_MOTION_DOT", description="First derivative of mean motion")
    mean_motion_ddot: Optional[float] = Field(default=0.0, alias="MEAN_MOTION_DDOT", description="Second derivative of mean motion")

    @field_validator("norad_cat_id")
    @classmethod
    def validate_norad_cat_id(cls, v: int) -> int:
        if v <= 0 or v > 999999999:
            raise ValueError(f"NORAD catalog ID must be between 1 and 999,999,999, got {v}")
        return v

    @field_validator("eccentricity")
    @classmethod
    def validate_eccentricity(cls, v: float) -> float:
        if v < 0 or v >= 1.0:
            raise ValueError(f"Eccentricity must be in range [0, 1.0) for closed orbits, got {v}")
        return v

    @field_validator("inclination")
    @classmethod
    def validate_inclination(cls, v: float) -> float:
        if v < 0.0 or v > 180.0:
            raise ValueError(f"Inclination must be in range [0.0, 180.0] degrees, got {v}")
        return v

    @field_validator("mean_motion")
    @classmethod
    def validate_mean_motion(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"Mean motion must be greater than 0, got {v}")
        return v

    @field_validator("bstar", "mean_motion_dot", "mean_motion_ddot", mode="before")
    @classmethod
    def coerce_none_floats(cls, v: Optional[float]) -> float:
        return 0.0 if v is None else float(v)

    @field_validator("ephemeris_type", mode="before")
    @classmethod
    def coerce_none_ephemeris(cls, v: Optional[int]) -> int:
        return 0 if v is None else int(v)

    @field_validator("classification_type", mode="before")
    @classmethod
    def coerce_none_classification(cls, v: Optional[str]) -> str:
        return "U" if v is None else str(v)

    def to_omm_dict(self) -> dict[str, Any]:
        """Convert record to a CCSDS OMM dictionary formatted for SGP4 ingestion."""
        data = self.model_dump(by_alias=True)

        # Normalize epoch to %Y-%m-%dT%H:%M:%S.%f format required by sgp4.omm
        cleaned = self.epoch.strip().rstrip("Z")
        try:
            dt = datetime.fromisoformat(cleaned)
            data["EPOCH"] = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
        except Exception:
            data["EPOCH"] = self.epoch

        # Fallbacks for optional integer fields
        if data.get("ELEMENT_SET_NO") is None:
            data["ELEMENT_SET_NO"] = 999
        if data.get("REV_AT_EPOCH") is None:
            data["REV_AT_EPOCH"] = 0

        return data


class StoredOrbitalRecord(OrbitalRecord):
    """Persistent orbital record with data provenance, storage timestamps, and epoch comparison metadata."""

    source: str = Field(
        default="celestrak",
        alias="SOURCE",
        description="Data source of the orbital elements (e.g. 'celestrak')",
    )
    retrieved_at: str = Field(
        alias="RETRIEVED_AT",
        description="UTC timestamp when record was retrieved and stored (ISO 8601)",
    )
    epoch_timestamp: float = Field(
        alias="EPOCH_TIMESTAMP",
        description="Numeric Unix timestamp (seconds) of the orbital epoch for temporal ordering",
    )

    def to_orbital_record(self) -> OrbitalRecord:
        """Convert to base OrbitalRecord without persistence metadata."""
        return OrbitalRecord.model_validate(self.model_dump())


class HistoricalOrbitalRecord(OrbitalRecord):
    """Historical orbital element set with full archival metadata and deduplication fingerprint."""

    id: Optional[int] = Field(default=None, description="Internal database surrogate ID")
    source: str = Field(
        default="space-track",
        alias="SOURCE",
        description="Origin provider (e.g. 'space-track', 'celestrak', 'bulk-zip')",
    )
    retrieved_at: Optional[str] = Field(
        default=None,
        alias="RETRIEVED_AT",
        description="UTC timestamp when record was ingested into SatView",
    )
    epoch_timestamp: Optional[float] = Field(
        default=None,
        alias="EPOCH_TIMESTAMP",
        description="Numeric Unix timestamp (seconds) of the orbital epoch",
    )
    creation_date: Optional[str] = Field(
        default=None,
        alias="CREATION_DATE",
        description="Upstream publication/generation timestamp from space surveillance network",
    )
    creation_date_timestamp: Optional[float] = Field(
        default=None,
        alias="CREATION_DATE_TIMESTAMP",
        description="Numeric Unix timestamp (seconds) of creation date",
    )
    gp_id: Optional[int] = Field(
        default=None,
        alias="GP_ID",
        description="Space-Track unique record identifier (int10 unsigned)",
    )
    fingerprint: Optional[str] = Field(
        default=None,
        description="Deterministic SHA-256 fingerprint of canonical orbital and source data",
    )

    # Optional Space-Track auxiliary metadata
    semimajor_axis: Optional[float] = Field(default=None, alias="SEMIMAJOR_AXIS")
    period: Optional[float] = Field(default=None, alias="PERIOD")
    apoapsis: Optional[float] = Field(default=None, alias="APOAPSIS")
    periapsis: Optional[float] = Field(default=None, alias="PERIAPSIS")
    decay_date: Optional[str] = Field(default=None, alias="DECAY_DATE")
    tle_line1: Optional[str] = Field(default=None, alias="TLE_LINE1")
    tle_line2: Optional[str] = Field(default=None, alias="TLE_LINE2")

    def to_orbital_record(self) -> OrbitalRecord:
        """Convert to base OrbitalRecord without historical persistence metadata."""
        return OrbitalRecord.model_validate(self.model_dump())


class IngestionCheckpoint(BaseModel):
    """Safe checkpoint tracker for local archival ingestion progress and recovery."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    norad_cat_id: int = Field(..., ge=1, le=999999999, description="NORAD Catalog Number")
    source: str = Field(..., description="Data source identifier (e.g. 'space-track', 'archive-zip')")
    status: str = Field(
        default="pending",
        description="Ingestion status: 'pending', 'in_progress', 'completed', 'failed'",
    )
    earliest_epoch: Optional[str] = Field(default=None, description="Earliest ingested epoch (ISO 8601)")
    earliest_epoch_timestamp: Optional[float] = Field(default=None, description="Unix timestamp of earliest epoch")
    latest_epoch: Optional[str] = Field(default=None, description="Latest ingested epoch (ISO 8601)")
    latest_epoch_timestamp: Optional[float] = Field(default=None, description="Unix timestamp of latest epoch")
    record_count: int = Field(default=0, ge=0, description="Total records imported for this satellite and source")
    last_error: Optional[str] = Field(default=None, description="Last recorded failure message if any")
    last_attempt_at: str = Field(..., description="UTC timestamp of last ingestion attempt")
    completed_at: Optional[str] = Field(default=None, description="UTC timestamp when full ingestion completed")


SelectionQuality = Literal["nominal", "degraded", "unreliable", "unavailable"]
OrbitalRegimeType = Literal["LEO", "MEO", "GEO", "OTHER"]


class RegimeThresholds(BaseModel):
    """Configurable operational staleness thresholds for an orbital regime."""

    nominal_hours: float = Field(..., gt=0, description="Nominal epoch distance threshold in hours")
    max_hours: float = Field(..., gt=0, description="Maximum operational epoch distance threshold in hours")

    @model_validator(mode="after")
    def validate_threshold_ordering(self) -> "RegimeThresholds":
        if self.nominal_hours > self.max_hours:
            raise ValueError(
                f"nominal_hours ({self.nominal_hours}) must be less than or equal to max_hours ({self.max_hours})"
            )
        return self


class SelectionPolicyConfig(BaseModel):
    """Configuration for historical orbital record selection policy."""

    policy_name: str = Field(default="conservative_prior", description="Identifier of the selection policy")
    leo_thresholds: RegimeThresholds = Field(
        default_factory=lambda: RegimeThresholds(nominal_hours=24.0, max_hours=72.0),
        description="Thresholds for Low Earth Orbit (period < 128 min, mean motion > 11.25)",
    )
    meo_thresholds: RegimeThresholds = Field(
        default_factory=lambda: RegimeThresholds(nominal_hours=168.0, max_hours=336.0),
        description="Thresholds for Medium Earth Orbit (default: 7 days nominal, 14 days max)",
    )
    geo_thresholds: RegimeThresholds = Field(
        default_factory=lambda: RegimeThresholds(nominal_hours=168.0, max_hours=336.0),
        description="Thresholds for Geostationary/Geosynchronous Orbit (default: 7 days nominal, 14 days max)",
    )
    default_thresholds: RegimeThresholds = Field(
        default_factory=lambda: RegimeThresholds(nominal_hours=24.0, max_hours=72.0),
        description=(
            "Default fallback thresholds when orbital regime is unclassified or classified as OTHER. "
            "Currently uses the conservative 24-hour nominal / 72-hour maximum policy as an operational "
            "fallback, not a scientific guarantee."
        ),
    )


class CandidateContext(BaseModel):
    """Metadata summary of an evaluated or alternative candidate element set."""

    record_id: Optional[int] = Field(default=None, description="Database surrogate ID")
    gp_id: Optional[int] = Field(default=None, description="Space-Track GP_ID")
    epoch: str = Field(..., description="Element epoch ISO 8601 UTC string")
    epoch_timestamp: float = Field(..., description="Numeric Unix timestamp of element epoch")
    creation_date: Optional[str] = Field(default=None, description="Creation timestamp if available")
    creation_date_timestamp: Optional[float] = Field(default=None, description="Creation Unix timestamp")
    source: str = Field(..., description="Record origin source identifier")
    time_difference_seconds: float = Field(
        ...,
        description="Signed time difference: requested_timestamp - epoch_timestamp (positive: prior, negative: subsequent)",
    )
    absolute_distance_hours: float = Field(..., description="Absolute distance between requested time and epoch in hours")
    is_prior: bool = Field(..., description="True if candidate epoch is at or before requested time")


class SelectionMetadata(BaseModel):
    """Auditing and diagnostic metadata describing selection execution and candidate context."""

    policy_name: str = Field(..., description="Selection policy applied")
    orbital_regime: str = Field(..., description="Detected or configured orbital regime (e.g. LEO, MEO, GEO, OTHER)")
    nominal_threshold_hours: float = Field(..., description="Nominal staleness threshold applied (hours)")
    max_threshold_hours: float = Field(..., description="Maximum operational staleness threshold applied (hours)")
    allow_unreliable_requested: bool = Field(..., description="Whether allow_unreliable flag was enabled")
    candidates_evaluated: int = Field(default=0, ge=0, description="Total number of candidates examined")
    subsequent_candidate: Optional[CandidateContext] = Field(
        default=None, description="Closest subsequent candidate retained as context"
    )
    alternative_prior_candidates_count: int = Field(
        default=0,
        ge=0,
        description="Count of additional prior candidates within the evaluated candidate window",
    )


class HistoricalSelectionRequest(BaseModel):
    """Request parameters for historical orbital element selection."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    norad_cat_id: int = Field(..., ge=1, le=999999999, description="NORAD Catalog Number")
    timestamp: str = Field(..., description="Requested UTC timestamp in ISO 8601 format")
    allow_unreliable: bool = Field(
        default=False,
        description="If True, return an otherwise stale prior record outside operational max with quality='unreliable'",
    )
    max_epoch_distance_hours: Optional[float] = Field(
        default=None, gt=0, description="Optional override for maximum acceptable epoch distance in hours"
    )
    nominal_threshold_hours: Optional[float] = Field(
        default=None, gt=0, description="Optional override for nominal threshold in hours"
    )
    orbital_regime: Optional[OrbitalRegimeType] = Field(
        default=None, description="Optional explicit regime override ('LEO', 'MEO', 'GEO', 'OTHER')"
    )

    @field_validator("orbital_regime", mode="before")
    @classmethod
    def validate_orbital_regime(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("orbital_regime must be a string")
        upper_v = v.upper().strip()
        if upper_v not in ("LEO", "MEO", "GEO", "OTHER"):
            raise ValueError(
                f"Invalid orbital_regime '{v}'. Must be one of: 'LEO', 'MEO', 'GEO', 'OTHER'"
            )
        return upper_v

    @model_validator(mode="after")
    def validate_threshold_overrides(self) -> "HistoricalSelectionRequest":
        if (
            self.nominal_threshold_hours is not None
            and self.max_epoch_distance_hours is not None
            and self.nominal_threshold_hours > self.max_epoch_distance_hours
        ):
            raise ValueError(
                f"nominal_threshold_hours ({self.nominal_threshold_hours}) must be less than or equal to "
                f"max_epoch_distance_hours ({self.max_epoch_distance_hours})"
            )
        return self


class HistoricalSelectionResult(BaseModel):
    """Result of historical orbital record selection including provenance, time difference, and quality."""

    norad_cat_id: int = Field(..., description="NORAD Catalog Number")
    requested_time: str = Field(..., description="Normalized requested UTC timestamp (ISO 8601)")
    requested_timestamp: float = Field(..., description="Unix timestamp of requested time")
    quality: str = Field(
        ...,
        description="Quality classification: 'nominal', 'degraded', 'unreliable', or 'unavailable'",
    )
    reason: str = Field(..., description="Explanatory description of the selection outcome")
    record: Optional[HistoricalOrbitalRecord] = Field(
        default=None, description="The selected HistoricalOrbitalRecord (None if unavailable)"
    )
    record_id: Optional[int] = Field(default=None, description="Database surrogate ID of selected record")
    gp_id: Optional[int] = Field(default=None, description="Space-Track GP_ID of selected record")
    element_epoch: Optional[str] = Field(default=None, description="Epoch timestamp of selected element set")
    creation_date: Optional[str] = Field(default=None, description="Creation timestamp of selected element set")
    source: Optional[str] = Field(default=None, description="Source provenance of selected element set")
    time_difference_seconds: Optional[float] = Field(
        default=None,
        description="Signed time difference: requested_timestamp - element_epoch_timestamp (positive: prior, negative: subsequent)",
    )
    absolute_distance_hours: Optional[float] = Field(
        default=None, description="Absolute epoch distance in hours"
    )
    is_prior: Optional[bool] = Field(
        default=None, description="True if selected record epoch is <= requested timestamp"
    )
    metadata: SelectionMetadata = Field(..., description="Detailed selection metadata and context")

