"""Orbit module domain exceptions."""


class OrbitError(Exception):
    """Base exception for all orbit module errors."""

    pass


class CelesTrakError(OrbitError):
    """Raised when CelesTrak API fails or returns unexpected responses."""

    pass


class CelesTrakRateLimitError(CelesTrakError):
    """Raised when CelesTrak returns HTTP 403 due to fair-use rate limits or 1-download-per-update policy."""

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


class PropagationError(OrbitError):
    """Raised when orbital propagation fails (e.g. satellite decay, singularity, or invalid time)."""

    pass


class ObserverError(OrbitError):
    """Raised when observer coordinates or topocentric geometry calculations fail."""

    pass


