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
