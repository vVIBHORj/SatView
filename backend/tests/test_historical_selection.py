"""Comprehensive tests for historical orbital record selection service."""

from datetime import datetime, timezone
from pydantic import ValidationError
import pytest

from app.orbit.exceptions import RecordValidationError
from app.orbit.models import (
    HistoricalOrbitalRecord,
    HistoricalSelectionRequest,
    RegimeThresholds,
    SelectionPolicyConfig,
)
from app.orbit.selection import HistoricalSelectionService, classify_orbital_regime
from app.orbit.storage import OrbitStorage, parse_epoch_timestamp


@pytest.fixture
def memory_storage() -> OrbitStorage:
    """Fixture providing an isolated in-memory OrbitStorage instance."""
    storage = OrbitStorage(db_url=":memory:")
    yield storage
    storage.close()


@pytest.fixture
def service(memory_storage: OrbitStorage) -> HistoricalSelectionService:
    """Fixture providing a HistoricalSelectionService backed by memory_storage."""
    return HistoricalSelectionService(storage=memory_storage)


def make_rec(
    norad_cat_id: int = 25544,
    object_name: str = "ISS (ZARYA)",
    object_id: str = "1998-067A",
    epoch: str = "2023-01-01T00:00:00Z",
    mean_motion: float = 15.5,
    eccentricity: float = 0.0001,
    inclination: float = 51.64,
    ra_of_asc_node: float = 120.0,
    arg_of_pericenter: float = 60.0,
    mean_anomaly: float = 30.0,
    gp_id: int | None = 1001,
    creation_date: str | None = None,
    source: str = "space-track:test",
) -> HistoricalOrbitalRecord:
    """Helper to construct HistoricalOrbitalRecord test instances."""
    return HistoricalOrbitalRecord(
        norad_cat_id=norad_cat_id,
        object_name=object_name,
        object_id=object_id,
        epoch=epoch,
        mean_motion=mean_motion,
        eccentricity=eccentricity,
        inclination=inclination,
        ra_of_asc_node=ra_of_asc_node,
        arg_of_pericenter=arg_of_pericenter,
        mean_anomaly=mean_anomaly,
        gp_id=gp_id,
        creation_date=creation_date,
        source=source,
    )


# ---------------------------------------------------------------------------
# 1. Exact Match & Causal Prior Preference Tests
# ---------------------------------------------------------------------------


def test_exact_epoch_match(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """Requested timestamp exactly matching element epoch produces nominal quality and zero time difference."""
    rec = make_rec(epoch="2023-06-01T12:00:00Z", gp_id=101)
    memory_storage.insert_history_record(rec)

    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res.quality == "nominal"
    assert res.record is not None
    assert res.gp_id == 101
    assert res.element_epoch == "2023-06-01T12:00:00Z"
    assert res.time_difference_seconds == 0.0
    assert res.absolute_distance_hours == 0.0
    assert res.is_prior is True
    assert "nominal" in res.reason.lower()


def test_prior_versus_subsequent_preference(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """Conservative policy prefers prior candidate even when a subsequent candidate is substantially closer."""
    # Prior candidate: 3 hours before requested time
    rec_prior = make_rec(epoch="2023-06-01T09:00:00Z", gp_id=201)
    # Subsequent candidate: only 15 minutes after requested time
    rec_subsequent = make_rec(epoch="2023-06-01T12:15:00Z", gp_id=202)

    memory_storage.batch_insert_history([rec_prior, rec_subsequent])

    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    # Must select prior record (201), NOT the closer subsequent record (202)
    assert res.record is not None
    assert res.gp_id == 201
    assert res.element_epoch == "2023-06-01T09:00:00Z"
    assert res.time_difference_seconds == 3.0 * 3600.0
    assert res.absolute_distance_hours == 3.0
    assert res.is_prior is True

    # Subsequent candidate must be retained as selection context
    assert res.metadata.subsequent_candidate is not None
    sub = res.metadata.subsequent_candidate
    assert sub.gp_id == 202
    assert sub.epoch == "2023-06-01T12:15:00Z"
    assert sub.time_difference_seconds == -15.0 * 60.0  # Negative signed diff
    assert sub.absolute_distance_hours == 0.25
    assert sub.is_prior is False


def test_closest_prior_record(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """Closest prior record is chosen when multiple prior candidates are available."""
    recs = [
        make_rec(epoch="2023-05-20T00:00:00Z", gp_id=301),  # 12 days prior
        make_rec(epoch="2023-05-30T00:00:00Z", gp_id=302),  # 2 days prior
        make_rec(epoch="2023-06-01T06:00:00Z", gp_id=303),  # 6 hours prior (closest)
    ]
    memory_storage.batch_insert_history(recs)

    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res.record is not None
    assert res.gp_id == 303
    assert res.element_epoch == "2023-06-01T06:00:00Z"
    assert res.absolute_distance_hours == 6.0
    assert res.metadata.alternative_prior_candidates_count == 2


# ---------------------------------------------------------------------------
# 2. Same Epoch Tie-Breaking & Missing Creation Date
# ---------------------------------------------------------------------------


def test_same_epoch_different_creation_dates(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """When multiple records share identical epoch, the one with latest creation_date is selected."""
    rec_early_pub = make_rec(
        epoch="2023-06-01T00:00:00Z",
        creation_date="2023-06-01T01:00:00Z",
        gp_id=401,
        mean_motion=15.5000,
    )
    rec_late_pub = make_rec(
        epoch="2023-06-01T00:00:00Z",
        creation_date="2023-06-01T04:00:00Z",  # 3 hours newer publication
        gp_id=402,
        mean_motion=15.5001,
    )
    memory_storage.batch_insert_history([rec_early_pub, rec_late_pub])

    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res.record is not None
    assert res.gp_id == 402
    assert res.creation_date == "2023-06-01T04:00:00Z"


def test_missing_creation_date_handled_deterministically(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """Record with known creation_date is preferred over missing creation_date at same epoch."""
    rec_missing = make_rec(
        epoch="2023-06-01T00:00:00Z",
        creation_date=None,
        gp_id=501,
        mean_motion=15.5000,
    )
    rec_known = make_rec(
        epoch="2023-06-01T00:00:00Z",
        creation_date="2023-06-01T02:00:00Z",
        gp_id=502,
        mean_motion=15.5001,
    )
    memory_storage.batch_insert_history([rec_missing, rec_known])

    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res.record is not None
    assert res.gp_id == 502
    assert res.creation_date == "2023-06-01T02:00:00Z"


def test_same_epoch_both_missing_creation_date_deterministic(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """When both records lack creation_date, deterministic tie-breaking produces consistent selection."""
    rec1 = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=601, creation_date=None, mean_motion=15.5000)
    rec2 = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=602, creation_date=None, mean_motion=15.5001)
    memory_storage.batch_insert_history([rec1, rec2])

    res1 = service.select_record(25544, "2023-06-01T12:00:00Z")
    res2 = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res1.gp_id == res2.gp_id
    assert res1.gp_id in (601, 602)


# ---------------------------------------------------------------------------
# 3. Staleness Thresholds: LEO vs MEO vs GEO
# ---------------------------------------------------------------------------


def test_leo_nominal_threshold(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """LEO record within 24 hours of requested time is classified as nominal."""
    rec = make_rec(epoch="2023-06-01T02:00:00Z", gp_id=701, mean_motion=15.5)  # LEO
    memory_storage.insert_history_record(rec)

    # 10 hours later
    res = service.select_record(25544, "2023-06-01T12:00:00Z")

    assert res.quality == "nominal"
    assert res.metadata.orbital_regime == "LEO"
    assert res.metadata.nominal_threshold_hours == 24.0
    assert res.metadata.max_threshold_hours == 72.0
    assert res.record is not None


def test_leo_degraded_threshold(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """LEO record between 24 and 72 hours of requested time is classified as degraded."""
    rec = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=702, mean_motion=15.5)  # LEO
    memory_storage.insert_history_record(rec)

    # 48 hours later (between 24h and 72h)
    res = service.select_record(25544, "2023-06-03T00:00:00Z")

    assert res.quality == "degraded"
    assert res.absolute_distance_hours == 48.0
    assert res.record is not None
    assert "exceeds nominal threshold but is within operational maximum" in res.reason


def test_leo_unavailable_threshold(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """LEO record older than 72 hours is classified as unavailable and returns no record when allow_unreliable=False."""
    rec = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=703, mean_motion=15.5)  # LEO
    memory_storage.insert_history_record(rec)

    # 80 hours later (> 72h maximum operational window)
    res = service.select_record(25544, "2023-06-04T08:00:00Z", allow_unreliable=False)

    assert res.quality == "unavailable"
    assert res.record is None
    assert res.record_id is None
    assert res.element_epoch is None
    assert "exceeds operational maximum" in res.reason


def test_geo_thresholds_not_subject_to_leo_24h_limit(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """GEO satellite is evaluated against GEO thresholds (nominal 7d, max 14d) rather than LEO 24h limit."""
    # GEO satellite (mean_motion ~ 1.002, ecc = 0.0001, e.g. GOES / Inmarsat)
    rec_geo = make_rec(
        norad_cat_id=40001,
        object_name="GOES TEST",
        epoch="2023-06-01T00:00:00Z",
        mean_motion=1.0027,
        eccentricity=0.0001,
        gp_id=801,
    )
    memory_storage.insert_history_record(rec_geo)

    # Case 1: 5 days later (120 hours) -> Should be NOMINAL for GEO (nominal is 7 days / 168 hours)
    res_5d = service.select_record(40001, "2023-06-06T00:00:00Z")
    assert res_5d.metadata.orbital_regime == "GEO"
    assert res_5d.metadata.nominal_threshold_hours == 168.0
    assert res_5d.quality == "nominal"
    assert res_5d.record is not None

    # Case 2: 10 days later (240 hours) -> DEGRADED for GEO (between 7d and 14d)
    res_10d = service.select_record(40001, "2023-06-11T00:00:00Z")
    assert res_10d.quality == "degraded"
    assert res_10d.record is not None

    # Case 3: 16 days later (384 hours) -> UNAVAILABLE for GEO (> 14d)
    res_16d = service.select_record(40001, "2023-06-17T00:00:00Z")
    assert res_16d.quality == "unavailable"
    assert res_16d.record is None


def test_custom_configurable_regime_thresholds(memory_storage: OrbitStorage) -> None:
    """Service supports user-configured policy thresholds across regimes."""
    custom_config = SelectionPolicyConfig(
        meo_thresholds=RegimeThresholds(nominal_hours=48.0, max_hours=96.0)
    )
    custom_service = HistoricalSelectionService(storage=memory_storage, config=custom_config)

    # GPS MEO satellite (mean motion ~ 2.005 rev/day)
    rec_meo = make_rec(
        norad_cat_id=28874,
        object_name="GPS BIIR-11",
        epoch="2023-06-01T00:00:00Z",
        mean_motion=2.005,
        eccentricity=0.008,
        gp_id=850,
    )
    memory_storage.insert_history_record(rec_meo)

    # At 60 hours: exceeds custom 48h nominal, within custom 96h max -> degraded
    res = custom_service.select_record(28874, "2023-06-03T12:00:00Z")
    assert res.metadata.orbital_regime == "MEO"
    assert res.metadata.nominal_threshold_hours == 48.0
    assert res.metadata.max_threshold_hours == 96.0
    assert res.quality == "degraded"


# ---------------------------------------------------------------------------
# 4. allow_unreliable Behavior
# ---------------------------------------------------------------------------


def test_allow_unreliable_returns_record_with_unreliable_quality(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """With allow_unreliable=True, stale prior record is returned with explicit 'unreliable' classification."""
    rec = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=901, mean_motion=15.5)  # LEO
    memory_storage.insert_history_record(rec)

    # 100 hours later (> 72h LEO max)
    req_time = "2023-06-05T04:00:00Z"

    # Default: allow_unreliable=False -> unavailable
    res_default = service.select_record(25544, req_time, allow_unreliable=False)
    assert res_default.quality == "unavailable"
    assert res_default.record is None

    # Opt-in: allow_unreliable=True -> unreliable, record returned
    res_opt_in = service.select_record(25544, req_time, allow_unreliable=True)
    assert res_opt_in.quality == "unreliable"
    assert res_opt_in.record is not None
    assert res_opt_in.gp_id == 901
    assert res_opt_in.element_epoch == "2023-06-01T00:00:00Z"
    assert res_opt_in.time_difference_seconds == 100.0 * 3600.0
    assert res_opt_in.absolute_distance_hours == 100.0
    assert res_opt_in.is_prior is True
    assert "quality='unreliable' because allow_unreliable=True" in res_opt_in.reason


# ---------------------------------------------------------------------------
# 5. Empty History, No Prior Records & Error Handling
# ---------------------------------------------------------------------------


def test_empty_history_returns_unavailable(service: HistoricalSelectionService) -> None:
    """Satellite with no records in history returns unavailable without raising exceptions."""
    res = service.select_record(99999, "2023-06-01T00:00:00Z")

    assert res.quality == "unavailable"
    assert res.record is None
    assert res.record_id is None
    assert "No historical orbital records found" in res.reason


def test_only_subsequent_records_available(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """When only subsequent records exist, returns unavailable while recording subsequent candidate context."""
    # Candidate in 2024
    rec = make_rec(epoch="2024-01-01T00:00:00Z", gp_id=1001)
    memory_storage.insert_history_record(rec)

    # Request in 2023
    res = service.select_record(25544, "2023-01-01T00:00:00Z")

    assert res.quality == "unavailable"
    assert res.record is None
    assert res.metadata.subsequent_candidate is not None
    assert res.metadata.subsequent_candidate.gp_id == 1001
    assert "Found closest subsequent record" in res.reason


def test_invalid_requested_timestamp_raises_validation_error(service: HistoricalSelectionService) -> None:
    """Malformed or unparseable requested timestamps raise RecordValidationError."""
    with pytest.raises(RecordValidationError, match="Invalid requested timestamp"):
        service.select_record(25544, "not-a-valid-timestamp")


# ---------------------------------------------------------------------------
# 6. Provenance & UTC Offset / Fractional Second Accuracy
# ---------------------------------------------------------------------------


def test_provenance_preservation(service: HistoricalSelectionService, memory_storage: OrbitStorage) -> None:
    """Selection result fully preserves GP_ID, source provenance, creation date, and element parameters."""
    rec = make_rec(
        norad_cat_id=25544,
        epoch="2023-06-01T00:00:00Z",
        gp_id=1101,
        creation_date="2023-06-01T02:30:00Z",
        source="space-track:bulk-archive-2023",
    )
    rec.tle_line1 = "1 25544U 98067A   23152.00000000  .00001000  00000-0  20000-4 0  9991"
    rec.tle_line2 = "2 25544  51.6400 120.0000 0001000  60.0000  30.0000 15.50000000 10002"
    rec.element_set_no = 999
    rec.rev_at_epoch = 14000
    memory_storage.insert_history_record(rec)

    res = service.select_record(25544, "2023-06-01T04:00:00Z")

    assert res.record is not None
    assert res.gp_id == 1101
    assert res.source == "space-track:bulk-archive-2023"
    assert res.creation_date == "2023-06-01T02:30:00Z"
    assert res.record.tle_line1 == rec.tle_line1
    assert res.record.tle_line2 == rec.tle_line2
    assert res.record.element_set_no == 999
    assert res.record.rev_at_epoch == 14000


def test_utc_offsets_and_fractional_second_accuracy(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """Non-UTC timezone offsets are normalized to UTC and fractional seconds are compared with microsecond accuracy."""
    # Element epoch with microseconds: 00:00:00.123456Z
    rec = make_rec(epoch="2023-06-01T00:00:00.123456Z", gp_id=1201)
    memory_storage.insert_history_record(rec)

    # Requested time provided with non-UTC offset (+05:30): 05:30:00.623456+05:30 -> 00:00:00.623456Z
    res = service.select_record(25544, "2023-06-01T05:30:00.623456+05:30")

    assert res.requested_time == "2023-06-01T00:00:00.623456Z"
    assert res.quality == "nominal"
    assert res.record is not None
    # Difference: 00:00:00.623456 - 00:00:00.123456 = 0.500000 seconds
    assert pytest.approx(res.time_difference_seconds, abs=1e-5) == 0.5


def test_deterministic_selection_repeated_calls(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """Fifty consecutive selection requests across a populated history produce identical results."""
    recs = [
        make_rec(epoch=f"2023-06-01T0{i}:00:00Z", gp_id=1300 + i)
        for i in range(8)
    ]
    memory_storage.batch_insert_history(recs)

    first_res = service.select_record(25544, "2023-06-01T06:30:00Z")

    for _ in range(50):
        repeat_res = service.select_record(25544, "2023-06-01T06:30:00Z")
        assert repeat_res.gp_id == first_res.gp_id
        assert repeat_res.time_difference_seconds == first_res.time_difference_seconds
        assert repeat_res.quality == first_res.quality


# ---------------------------------------------------------------------------
# 7. Request Overrides for Regime and Thresholds
# ---------------------------------------------------------------------------


def test_request_regime_and_threshold_overrides(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """Request-level overrides for orbital regime and thresholds take precedence."""
    rec = make_rec(epoch="2023-06-01T00:00:00Z", gp_id=1401, mean_motion=15.5)  # Natural LEO
    memory_storage.insert_history_record(rec)

    # Override nominal to 2 hours and max to 5 hours
    req = HistoricalSelectionRequest(
        norad_cat_id=25544,
        timestamp="2023-06-01T04:00:00Z",  # 4 hours later
        nominal_threshold_hours=2.0,
        max_epoch_distance_hours=5.0,
    )
    res = service.select(req)

    # 4h distance > 2h nominal, but <= 5h max -> degraded
    assert res.quality == "degraded"
    assert res.metadata.nominal_threshold_hours == 2.0
    assert res.metadata.max_threshold_hours == 5.0


# ---------------------------------------------------------------------------
# 8. Task 1.6.3.1 Policy Hardening Tests
# ---------------------------------------------------------------------------


def test_more_than_50_same_epoch_gp_id_tie_break_sql_limit(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """With >50 records sharing identical epoch and no creation date, the SQL ordering ensures the highest GP_ID winner is returned."""
    epoch = "2023-06-01T00:00:00Z"
    # Insert 60 records sharing the exact same epoch
    # Record 1 is inserted first (surrogate id = 1), but carries the highest gp_id (999999)
    # Records 2..60 have lower gp_id values
    recs = []
    for i in range(1, 61):
        recs.append(
            make_rec(
                epoch=epoch,
                gp_id=999999 if i == 1 else 1000 + i,
                creation_date=None,
                mean_motion=15.5,
            )
        )
    memory_storage.batch_insert_history(recs)

    res = service.select_record(25544, "2023-06-01T04:00:00Z")

    assert res.record is not None
    # Highest GP_ID (999999) must be selected despite being inserted first (id=1)
    assert res.gp_id == 999999
    assert res.record_id == 1


def test_subsequent_candidates_same_epoch_tie_break(
    service: HistoricalSelectionService, memory_storage: OrbitStorage
) -> None:
    """Subsequent candidate query orders by epoch ASC, then creation_date DESC, gp_id DESC."""
    epoch_future = "2023-06-02T00:00:00Z"
    rec_old_pub = make_rec(
        epoch=epoch_future,
        creation_date="2023-06-02T01:00:00Z",
        gp_id=2001,
    )
    rec_new_pub = make_rec(
        epoch=epoch_future,
        creation_date="2023-06-02T05:00:00Z",
        gp_id=2002,
    )
    memory_storage.batch_insert_history([rec_old_pub, rec_new_pub])

    res = service.select_record(25544, "2023-06-01T00:00:00Z")

    assert res.quality == "unavailable"
    assert res.metadata.subsequent_candidate is not None
    # Context must select rec_new_pub because it has the newer creation date
    assert res.metadata.subsequent_candidate.gp_id == 2002
    assert res.metadata.subsequent_candidate.creation_date == "2023-06-02T05:00:00Z"


def test_classify_orbital_regime_molniya_and_high_eccentricity() -> None:
    """High-eccentricity orbits classify as OTHER rather than receiving lenient MEO or GEO thresholds."""
    # 1. Molniya orbit: ~12-hour period (mean_motion ~ 2.006 rev/day), high eccentricity (e ~ 0.74)
    rec_molniya = make_rec(mean_motion=2.006, eccentricity=0.74)
    assert classify_orbital_regime(rec_molniya) == "OTHER"

    # 2. Tundra / high-eccentricity GEO-like orbit: mean_motion ~ 1.002, eccentricity = 0.27 (>= 0.2)
    rec_tundra = make_rec(mean_motion=1.002, eccentricity=0.27)
    assert classify_orbital_regime(rec_tundra) == "OTHER"

    # 3. MEO boundary at eccentricity = 0.25 (must be < 0.25 for MEO)
    rec_meo_ecc_boundary = make_rec(mean_motion=2.005, eccentricity=0.25)
    assert classify_orbital_regime(rec_meo_ecc_boundary) == "OTHER"

    # 4. GEO boundary at eccentricity = 0.20 (must be < 0.20 for GEO)
    rec_geo_ecc_boundary = make_rec(mean_motion=1.002, eccentricity=0.20)
    assert classify_orbital_regime(rec_geo_ecc_boundary) == "OTHER"


def test_classify_orbital_regime_standard_regimes() -> None:
    """Standard low-eccentricity LEO, MEO, and GEO satellites classify correctly."""
    # LEO: mean_motion > 11.25
    rec_leo = make_rec(mean_motion=15.5, eccentricity=0.0001)
    assert classify_orbital_regime(rec_leo) == "LEO"

    # MEO: 1.05 < mean_motion <= 11.25 and eccentricity < 0.25 (e.g. GPS, Galileo)
    rec_meo = make_rec(mean_motion=2.005, eccentricity=0.008)
    assert classify_orbital_regime(rec_meo) == "MEO"

    # GEO: 0.95 <= mean_motion <= 1.05 and eccentricity < 0.2 (e.g. GOES)
    rec_geo = make_rec(mean_motion=1.0027, eccentricity=0.0001)
    assert classify_orbital_regime(rec_geo) == "GEO"


def test_request_regime_validation() -> None:
    """HistoricalSelectionRequest validates orbital_regime against allowed regimes."""
    # Valid regimes (case-insensitive)
    for regime in ("LEO", "MEO", "GEO", "OTHER", "leo", "meo", "geo", "other"):
        req = HistoricalSelectionRequest(
            norad_cat_id=25544,
            timestamp="2023-06-01T00:00:00Z",
            orbital_regime=regime,  # type: ignore[arg-type]
        )
        assert req.orbital_regime == regime.upper()

    # None is valid
    req_none = HistoricalSelectionRequest(norad_cat_id=25544, timestamp="2023-06-01T00:00:00Z")
    assert req_none.orbital_regime is None

    # Invalid regimes raise ValidationError
    for invalid in ("INVALID", "MARS", "HEO", "GTO", "123"):
        with pytest.raises(ValidationError):
            HistoricalSelectionRequest(
                norad_cat_id=25544,
                timestamp="2023-06-01T00:00:00Z",
                orbital_regime=invalid,  # type: ignore[arg-type]
            )


def test_regime_thresholds_validation_boundaries_and_inversion() -> None:
    """RegimeThresholds validates positivity and ordering (nominal_hours <= max_hours)."""
    # Valid threshold boundaries
    th_valid = RegimeThresholds(nominal_hours=24.0, max_hours=72.0)
    assert th_valid.nominal_hours == 24.0
    assert th_valid.max_hours == 72.0

    # Equal thresholds are valid
    th_equal = RegimeThresholds(nominal_hours=48.0, max_hours=48.0)
    assert th_equal.nominal_hours == th_equal.max_hours == 48.0

    # Inverted thresholds raise ValidationError
    with pytest.raises(ValidationError, match="nominal_hours .* must be less than or equal to max_hours"):
        RegimeThresholds(nominal_hours=100.0, max_hours=50.0)

    # Zero or negative values raise ValidationError
    with pytest.raises(ValidationError):
        RegimeThresholds(nominal_hours=0.0, max_hours=72.0)

    with pytest.raises(ValidationError):
        RegimeThresholds(nominal_hours=24.0, max_hours=-10.0)


def test_request_threshold_overrides_inversion_validation() -> None:
    """HistoricalSelectionRequest rejects inverted threshold overrides."""
    with pytest.raises(ValidationError, match="nominal_threshold_hours .* must be less than or equal to"):
        HistoricalSelectionRequest(
            norad_cat_id=25544,
            timestamp="2023-06-01T00:00:00Z",
            nominal_threshold_hours=100.0,
            max_epoch_distance_hours=50.0,
        )
