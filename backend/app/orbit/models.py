from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
