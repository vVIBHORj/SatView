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


class PassPredictionError(OrbitError):
    """Raised when pass prediction fails (e.g. invalid time window or numerical divergence)."""

    pass


class DatabaseError(OrbitError):
    """Raised when persistent database storage operations fail."""

    pass


class ArchiveError(OrbitError):
    """Base exception for historical archive parsing and ingestion errors."""

    pass


class ArchiveFormatError(ArchiveError):
    """Raised when an archive format is unsupported, malformed, or empty."""

    pass


class ArchiveSecurityError(ArchiveError):
    """Raised when an archive contains unsafe paths (e.g. path traversal or absolute paths)."""

    pass


class HistoricalSelectionError(OrbitError):
    """Base exception for historical orbital element selection errors."""

    pass


class HistoricalRecordNotFoundError(HistoricalSelectionError):
    """Raised when no historical orbital records exist matching selection criteria."""

    pass




