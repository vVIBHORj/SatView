"""Local historical orbit archive parser and ingestion engine."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
from pathlib import Path
from typing import Any, Generator, Optional
import zipfile

from pydantic import BaseModel, Field, ValidationError

from app.orbit.exceptions import (
    ArchiveError,
    ArchiveFormatError,
    ArchiveSecurityError,
    RecordValidationError,
)
from app.orbit.models import HistoricalOrbitalRecord, IngestionCheckpoint
from app.orbit.storage import OrbitStorage, parse_epoch_timestamp

logger = logging.getLogger(__name__)


class ArchiveParseError(BaseModel):
    """Structured parsing error identifying entry, row, and reason."""

    entry_name: str = Field(description="Name of the archive entry or file")
    row_number: Optional[int] = Field(default=None, description="1-indexed row or record number")
    raw_record: Optional[dict[str, Any]] = Field(default=None, description="Raw unparsed record data")
    error_message: str = Field(description="Descriptive error explanation")


class IngestionResult(BaseModel):
    """Summary result of a local archive ingestion operation."""

    archive_path: str = Field(description="Path to the ingested archive")
    source: str = Field(description="Qualified source identifier used for storage and checkpoints")
    total_records_read: int = Field(default=0, description="Total records/rows encountered in the archive")
    total_inserted: int = Field(default=0, description="Count of new records successfully committed")
    total_skipped: int = Field(default=0, description="Count of duplicate records skipped")
    satellites_affected: list[int] = Field(default_factory=list, description="List of NORAD catalog IDs imported")
    checkpoints: dict[int, IngestionCheckpoint] = Field(
        default_factory=dict, description="Checkpoints keyed by NORAD ID"
    )
    errors: list[ArchiveParseError] = Field(
        default_factory=list, description="List of parsing or row-level errors"
    )
    status: str = Field(
        default="completed", description="Overall ingestion status: 'completed', 'failed', or 'partial'"
    )


def compute_file_sha256(path: Path) -> str:
    """Compute deterministic SHA-256 hex digest of a local archive file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_zip_entry_safety(filename: str) -> None:
    """Validate that a zip entry path does not attempt path traversal or absolute file escaping."""
    if not filename:
        raise ArchiveSecurityError("Empty zip entry filename.")
    if "\0" in filename:
        raise ArchiveSecurityError(f"Zip entry contains null byte: '{filename}'")
    if filename.startswith("/") or filename.startswith("\\"):
        raise ArchiveSecurityError(f"Zip entry has unsafe absolute path: '{filename}'")
    if len(filename) >= 2 and filename[1] == ":":
        raise ArchiveSecurityError(f"Zip entry has unsafe Windows drive path: '{filename}'")
    norm_filename = filename.replace("\\", "/")
    parts = Path(norm_filename).parts
    if ".." in parts:
        raise ArchiveSecurityError(f"Zip entry attempts path traversal ('..'): '{filename}'")


def normalize_iso_timestamp(ts_str: Optional[str]) -> Optional[str]:
    """Normalize a date/timestamp string into canonical ISO 8601 UTC format."""
    if not ts_str:
        return None
    cleaned = str(ts_str).strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    ts = parse_epoch_timestamp(cleaned)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt.microsecond > 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_record_dict(raw: dict[str, Any], default_source: str = "space-track") -> dict[str, Any]:
    """Clean and sanitize raw dictionary keys and values from CSV/JSON exports."""
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        key = k.strip().upper()
        if isinstance(v, str):
            v_str = v.strip()
            if not v_str or v_str.lower() in ("null", "none", "n/a"):
                cleaned[key] = None
            else:
                cleaned[key] = v_str
        else:
            cleaned[key] = v

    # Normalize timestamps
    if "EPOCH" in cleaned and cleaned["EPOCH"]:
        cleaned["EPOCH"] = normalize_iso_timestamp(cleaned["EPOCH"])
    if "CREATION_DATE" in cleaned and cleaned["CREATION_DATE"]:
        cleaned["CREATION_DATE"] = normalize_iso_timestamp(cleaned["CREATION_DATE"])

    if "SOURCE" not in cleaned or not cleaned["SOURCE"]:
        cleaned["SOURCE"] = default_source

    return cleaned


class HistoricalArchiveReader:
    """Reader for local Space-Track GP_HISTORY JSON, CSV, and ZIP archives."""

    SUPPORTED_EXTENSIONS = {".json", ".csv", ".zip"}
    MAX_ZIP_MEMBERS: int = 10_000
    MAX_UNCOMPRESSED_ENTRY_BYTES: int = 250 * 1024 * 1024  # 250 MB

    def iter_archive(
        self, archive_path: Path | str, default_source: str = "space-track"
    ) -> Generator[tuple[Optional[HistoricalOrbitalRecord], Optional[ArchiveParseError]], None, None]:
        """Stream parsed records or errors from a local archive file."""
        path = Path(archive_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Archive file not found: {path}")

        ext = path.suffix.lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ArchiveFormatError(
                f"Unsupported archive format '{ext}'. Must be one of: {sorted(self.SUPPORTED_EXTENSIONS)}"
            )

        if path.stat().st_size == 0:
            raise ArchiveFormatError(f"Archive file '{path.name}' is empty (0 bytes).")

        if ext == ".csv":
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                yield from self._iter_csv_stream(f, entry_name=path.name, default_source=default_source)
        elif ext == ".json":
            with open(path, "r", encoding="utf-8-sig") as f:
                yield from self._iter_json_stream(f, entry_name=path.name, default_source=default_source)
        elif ext == ".zip":
            yield from self._iter_zip(path, default_source=default_source)

    def parse_archive(
        self, archive_path: Path | str, default_source: str = "space-track"
    ) -> tuple[list[HistoricalOrbitalRecord], list[ArchiveParseError]]:
        """Parse entire archive and return all valid records and parse errors."""
        records: list[HistoricalOrbitalRecord] = []
        errors: list[ArchiveParseError] = []
        for rec, err in self.iter_archive(archive_path, default_source=default_source):
            if rec is not None:
                records.append(rec)
            if err is not None:
                errors.append(err)
        return records, errors

    def _iter_csv_stream(
        self, text_stream: io.TextIOBase, entry_name: str, default_source: str
    ) -> Generator[tuple[Optional[HistoricalOrbitalRecord], Optional[ArchiveParseError]], None, None]:
        try:
            reader = csv.DictReader(text_stream)
            if reader.fieldnames is None:
                return
            for row_idx, row in enumerate(reader, start=2):  # line 1 is header
                try:
                    cleaned = clean_record_dict(row, default_source=default_source)
                    record = HistoricalOrbitalRecord.model_validate(cleaned)
                    yield record, None
                except (ValidationError, RecordValidationError, ValueError, TypeError, KeyError) as exc:
                    err = ArchiveParseError(
                        entry_name=entry_name,
                        row_number=row_idx,
                        raw_record=row,
                        error_message=str(exc),
                    )
                    yield None, err
        except Exception as exc:
            yield None, ArchiveParseError(
                entry_name=entry_name,
                row_number=None,
                raw_record=None,
                error_message=f"Failed to read CSV stream: {exc}",
            )

    def _iter_json_stream(
        self, text_stream: io.TextIOBase, entry_name: str, default_source: str
    ) -> Generator[tuple[Optional[HistoricalOrbitalRecord], Optional[ArchiveParseError]], None, None]:
        content = text_stream.read().strip()
        if not content:
            return

        if content.startswith("["):
            try:
                data = json.loads(content)
            except Exception as exc:
                yield None, ArchiveParseError(
                    entry_name=entry_name,
                    row_number=None,
                    raw_record=None,
                    error_message=f"JSON array decode error: {exc}",
                )
                return

            if not isinstance(data, list):
                yield None, ArchiveParseError(
                    entry_name=entry_name,
                    row_number=None,
                    raw_record=None,
                    error_message="Root JSON content must be an array of records",
                )
                return

            for idx, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    yield None, ArchiveParseError(
                        entry_name=entry_name,
                        row_number=idx,
                        raw_record=item if isinstance(item, dict) else {"raw": str(item)},
                        error_message="Record item must be a JSON object",
                    )
                    continue
                try:
                    cleaned = clean_record_dict(item, default_source=default_source)
                    record = HistoricalOrbitalRecord.model_validate(cleaned)
                    yield record, None
                except (ValidationError, RecordValidationError, ValueError, TypeError, KeyError) as exc:
                    yield None, ArchiveParseError(
                        entry_name=entry_name,
                        row_number=idx,
                        raw_record=item,
                        error_message=str(exc),
                    )
        else:
            # Try single object first
            try:
                single_obj = json.loads(content)
                if isinstance(single_obj, dict):
                    cleaned = clean_record_dict(single_obj, default_source=default_source)
                    record = HistoricalOrbitalRecord.model_validate(cleaned)
                    yield record, None
                    return
            except json.JSONDecodeError:
                pass

            # Parse line by line (NDJSON)
            lines = content.splitlines()
            for row_idx, line in enumerate(lines, start=1):
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    item = json.loads(line_str)
                    if not isinstance(item, dict):
                        yield None, ArchiveParseError(
                            entry_name=entry_name,
                            row_number=row_idx,
                            raw_record={"line": line_str},
                            error_message="Line must be a JSON object",
                        )
                        continue
                    cleaned = clean_record_dict(item, default_source=default_source)
                    record = HistoricalOrbitalRecord.model_validate(cleaned)
                    yield record, None
                except Exception as exc:
                    yield None, ArchiveParseError(
                        entry_name=entry_name,
                        row_number=row_idx,
                        raw_record={"line": line_str},
                        error_message=str(exc),
                    )

    def _iter_zip(
        self, archive_path: Path, default_source: str
    ) -> Generator[tuple[Optional[HistoricalOrbitalRecord], Optional[ArchiveParseError]], None, None]:
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                infolist = zf.infolist()
                if not infolist:
                    raise ArchiveFormatError(f"ZIP archive '{archive_path.name}' is empty.")

                if len(infolist) > self.MAX_ZIP_MEMBERS:
                    raise ArchiveSecurityError(
                        f"ZIP archive '{archive_path.name}' exceeds maximum member limit "
                        f"({len(infolist)} > {self.MAX_ZIP_MEMBERS})."
                    )

                supported_count = 0
                for info in infolist:
                    validate_zip_entry_safety(info.filename)
                    if info.is_dir():
                        continue

                    if info.file_size > self.MAX_UNCOMPRESSED_ENTRY_BYTES:
                        raise ArchiveSecurityError(
                            f"ZIP entry '{info.filename}' exceeds uncompressed size limit "
                            f"({info.file_size} > {self.MAX_UNCOMPRESSED_ENTRY_BYTES} bytes)."
                        )

                    lower_name = info.filename.lower()
                    if lower_name.endswith(".csv"):
                        supported_count += 1
                        with zf.open(info, "r") as raw_f:
                            with io.TextIOWrapper(raw_f, encoding="utf-8-sig") as tf:
                                yield from self._iter_csv_stream(
                                    tf, entry_name=info.filename, default_source=default_source
                                )
                    elif lower_name.endswith(".json"):
                        supported_count += 1
                        with zf.open(info, "r") as raw_f:
                            with io.TextIOWrapper(raw_f, encoding="utf-8-sig") as tf:
                                yield from self._iter_json_stream(
                                    tf, entry_name=info.filename, default_source=default_source
                                )

                if supported_count == 0:
                    raise ArchiveFormatError(
                        f"ZIP archive '{archive_path.name}' contains no supported .json or .csv entries."
                    )
        except zipfile.BadZipFile as exc:
            raise ArchiveFormatError(f"Invalid or corrupted ZIP archive '{archive_path.name}': {exc}") from exc


class HistoricalArchiveIngestor:
    """Service orchestrating parsing, batch insertion, and checkpoint tracking for local archives."""

    def __init__(self, storage: OrbitStorage, reader: Optional[HistoricalArchiveReader] = None):
        self.storage = storage
        self.reader = reader or HistoricalArchiveReader()

    def ingest_archive(
        self,
        archive_path: Path | str,
        source: Optional[str] = None,
        batch_size: int = 500,
    ) -> IngestionResult:
        """Ingest a local historical archive into OrbitStorage with atomic batching and checkpointing."""
        path = Path(archive_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Archive file not found: {path}")

        if source and source.strip():
            qualified_source = source.strip()
        else:
            file_hash = compute_file_sha256(path)[:12]
            qualified_source = f"space-track:gp-history:{path.stem.lower()}:{file_hash}"

        result = IngestionResult(
            archive_path=str(path),
            source=qualified_source,
        )

        records_batch: list[HistoricalOrbitalRecord] = []
        satellites_seen: set[int] = set()

        def flush_batch() -> None:
            nonlocal records_batch
            if not records_batch:
                return

            inserted, skipped = self.storage.batch_insert_history(records_batch, source=qualified_source)
            result.total_inserted += inserted
            result.total_skipped += skipped

            # Update in_progress checkpoints for satellites in this batch
            batch_satellites = {r.norad_cat_id for r in records_batch}
            for sat_id in batch_satellites:
                sat_recs = [r for r in records_batch if r.norad_cat_id == sat_id]
                earliest_rec = min(sat_recs, key=lambda r: parse_epoch_timestamp(r.epoch))
                latest_rec = max(sat_recs, key=lambda r: parse_epoch_timestamp(r.epoch))
                earliest_batch = earliest_rec.epoch
                latest_batch = latest_rec.epoch
                earliest_ts = parse_epoch_timestamp(earliest_rec.epoch)
                latest_ts = parse_epoch_timestamp(latest_rec.epoch)

                total_stored = self.storage.count_history_records(
                    norad_cat_id=sat_id, source=qualified_source
                )

                cp = IngestionCheckpoint(
                    norad_cat_id=sat_id,
                    source=qualified_source,
                    status="in_progress",
                    earliest_epoch=earliest_batch,
                    earliest_epoch_timestamp=earliest_ts,
                    latest_epoch=latest_batch,
                    latest_epoch_timestamp=latest_ts,
                    record_count=total_stored,
                    last_attempt_at=datetime.now(timezone.utc).isoformat(),
                )
                self.storage.save_checkpoint(cp)
                result.checkpoints[sat_id] = self.storage.get_checkpoint(sat_id, qualified_source) or cp

            records_batch.clear()

        try:
            for rec, err in self.reader.iter_archive(path, default_source=qualified_source):
                if err is not None:
                    result.errors.append(err)
                if rec is not None:
                    result.total_records_read += 1
                    satellites_seen.add(rec.norad_cat_id)
                    records_batch.append(rec)
                    if len(records_batch) >= batch_size:
                        flush_batch()

            # Flush remaining records
            if records_batch:
                flush_batch()

        except Exception as exc:
            logger.error(f"Fatal error during archive ingestion of '{path}': {exc}", exc_info=True)
            result.status = "failed"
            now_iso = datetime.now(timezone.utc).isoformat()
            for sat_id in satellites_seen:
                total_stored = self.storage.count_history_records(
                    norad_cat_id=sat_id, source=qualified_source
                )
                cp = IngestionCheckpoint(
                    norad_cat_id=sat_id,
                    source=qualified_source,
                    status="failed",
                    record_count=total_stored,
                    last_error=str(exc),
                    last_attempt_at=now_iso,
                )
                self.storage.save_checkpoint(cp)
                result.checkpoints[sat_id] = self.storage.get_checkpoint(sat_id, qualified_source) or cp
            raise

        result.satellites_affected = sorted(satellites_seen)

        # Finalize checkpoints
        now_iso = datetime.now(timezone.utc).isoformat()
        has_errors = len(result.errors) > 0

        if has_errors:
            result.status = "failed" if result.total_inserted == 0 and result.total_skipped == 0 else "partial"
            for sat_id in satellites_seen:
                total_stored = self.storage.count_history_records(
                    norad_cat_id=sat_id, source=qualified_source
                )
                cp = IngestionCheckpoint(
                    norad_cat_id=sat_id,
                    source=qualified_source,
                    status="failed",
                    record_count=total_stored,
                    last_error=f"{len(result.errors)} record parse error(s) in archive",
                    last_attempt_at=now_iso,
                )
                self.storage.save_checkpoint(cp)
                result.checkpoints[sat_id] = self.storage.get_checkpoint(sat_id, qualified_source) or cp
        else:
            result.status = "completed"
            for sat_id in satellites_seen:
                total_stored = self.storage.count_history_records(
                    norad_cat_id=sat_id, source=qualified_source
                )
                cp = IngestionCheckpoint(
                    norad_cat_id=sat_id,
                    source=qualified_source,
                    status="completed",
                    record_count=total_stored,
                    last_attempt_at=now_iso,
                    completed_at=now_iso,
                )
                self.storage.save_checkpoint(cp)
                result.checkpoints[sat_id] = self.storage.get_checkpoint(sat_id, qualified_source) or cp

        return result
