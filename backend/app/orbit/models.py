from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    # Optional / auxiliary fields in standard GP element sets
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
        if v <= 0:
            raise ValueError(f"NORAD catalog ID must be positive, got {v}")
        return v

    @field_validator("eccentricity")
    @classmethod
    def validate_eccentricity(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"Eccentricity cannot be negative, got {v}")
        return v

    @field_validator("mean_motion")
    @classmethod
    def validate_mean_motion(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"Mean motion must be greater than 0, got {v}")
        return v
