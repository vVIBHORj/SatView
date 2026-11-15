"""Orbit module domain exceptions."""


class OrbitError(Exception):
    """Base exception for all orbit module errors."""

    pass


class CelesTrakError(OrbitError):
    """Raised when CelesTrak API fails or returns unexpected responses."""

    pass


class CelesTrakTimeoutError(CelesTrakError):
    """Raised when a request to CelesTrak times out."""

    pass


class SatelliteNotFoundError(OrbitError):
    """Raised when a requested satellite or group has no orbital records."""

    pass


class RecordValidationError(OrbitError):
    """Raised when an orbital record fails schema validation."""

    pass
