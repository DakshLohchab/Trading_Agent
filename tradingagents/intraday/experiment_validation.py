"""Fail-closed verification helpers for reproducible historical experiments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.intraday.ingest import file_sha256, read_market_data
from tradingagents.intraday.models import InstrumentMetadata


class DatasetVerificationError(ValueError):
    """A dataset failed provenance, checksum, schema, or quality preflight."""


@dataclass(frozen=True, slots=True)
class VerifiedDataset:
    manifest_path: str
    dataset_id: str
    instrument_id: str
    actual_start_utc: str
    actual_end_utc: str
    record_counts: dict[str, int]
    artifact_sha256: dict[str, str]
    raw_sha256: dict[str, str]
    missing_intervals: int
    invalid_records: int
    duplicate_timestamps: int
    out_of_order_timestamps: int
    zero_volume_intervals: int


def verify_dataset_manifest(
    manifest_path: str | Path,
    instrument: InstrumentMetadata,
    *,
    expected_dataset_id: str | None = None,
    expected_counts: dict[str, int] | None = None,
    expected_actual_range_utc: tuple[str, str] | None = None,
) -> VerifiedDataset:
    """Verify a persisted dataset without altering, sorting, or repairing it."""
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetVerificationError("manifest is missing or invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise DatasetVerificationError("manifest root must be an object")
    dataset_id = manifest.get("dataset_id")
    if not dataset_id or expected_dataset_id and dataset_id != expected_dataset_id:
        raise DatasetVerificationError("dataset ID does not match the expected snapshot")
    if manifest.get("instrument_id") != instrument.instrument_id:
        raise DatasetVerificationError("manifest instrument identity does not match")
    if manifest.get("interval") != "1m":
        raise DatasetVerificationError("source dataset interval must be 1m")
    if manifest.get("transformation_version") != "tradingagents-intraday-dataset-v1":
        raise DatasetVerificationError("unknown dataset transformation version")
    raw_refs = manifest.get("raw_payloads")
    artifacts = manifest.get("artifacts")
    if not isinstance(raw_refs, list) or not raw_refs or not isinstance(artifacts, dict):
        raise DatasetVerificationError("manifest is missing raw or normalized artifact references")
    raw_hashes = {}
    raw_contents: dict[str, bytes] = {}
    for entry in raw_refs:
        raw_path = Path(entry.get("path", ""))
        if not raw_path.is_file() or file_sha256(raw_path) != entry.get("sha256"):
            raise DatasetVerificationError(f"raw artifact checksum failed: {raw_path.name}")
        raw_hashes[entry["name"]] = entry["sha256"]
        raw_contents[raw_path.name] = raw_path.read_bytes()
    zip_files = [name for name in raw_contents if name.endswith(".zip")]
    sidecars = [name for name in raw_contents if name.endswith(".zip.CHECKSUM")]
    if zip_files and sidecars:
        sidecar_digest = raw_contents[sidecars[0]].decode("ascii", errors="strict").split()[0]
        if hashlib.sha256(raw_contents[zip_files[0]]).hexdigest().lower() != sidecar_digest.lower():
            raise DatasetVerificationError("source archive does not match its checksum sidecar")
    artifact_hashes: dict[str, str] = {}
    record_counts: dict[str, int] = {}
    loaded_frames: dict[str, pd.DataFrame] = {}
    for key, entry in artifacts.items():
        artifact_path = Path(entry.get("path", ""))
        if not artifact_path.is_file() or file_sha256(artifact_path) != entry.get("sha256"):
            raise DatasetVerificationError(f"normalized artifact checksum failed: {key}")
        frame = pd.read_parquet(artifact_path)
        count = len(frame)
        if count != entry.get("record_count"):
            raise DatasetVerificationError(f"normalized artifact row count mismatch: {key}")
        interval = key.split(":", 1)[0]
        record_counts[interval] = record_counts.get(interval, 0) + count
        artifact_hashes[key] = entry["sha256"]
        loaded_frames[key] = frame
        if interval in ("5m", "15m") and ("is_valid" not in frame or not frame["is_valid"].all()):
            raise DatasetVerificationError(f"derived artifact contains invalid candles: {key}")
    counts = manifest.get("interval_record_counts")
    if counts != record_counts:
        raise DatasetVerificationError("manifest interval counts disagree with Parquet artifacts")
    if expected_counts and counts != expected_counts:
        raise DatasetVerificationError("dataset counts do not match the requested experiment snapshot")
    one_minute = loaded_frames.get("1m:2025-01")
    if one_minute is None:
        raise DatasetVerificationError("expected normalized 1m January 2025 artifact is absent")
    actual_start = pd.to_datetime(one_minute["timestamp"], utc=True).min().to_pydatetime()
    actual_end = pd.to_datetime(one_minute["timestamp"], utc=True).max().to_pydatetime()
    manifest_range = manifest.get("actual_available_range_utc")
    if not manifest_range or actual_start.isoformat() != manifest_range[0] or actual_end.isoformat() != manifest_range[1]:
        raise DatasetVerificationError("manifest date range disagrees with normalized timestamps")
    if expected_actual_range_utc and tuple(manifest_range) != expected_actual_range_utc:
        raise DatasetVerificationError("dataset range does not match the requested experiment window")
    ingestion = read_market_data(one_minute_path(artifacts), instrument)
    quality = manifest.get("quality_report", {})
    if len(ingestion.bars) != manifest.get("record_count"):
        raise DatasetVerificationError("manifest source record count disagrees with canonical ingestion")
    if ingestion.gaps or quality.get("missing_intervals") != 0:
        raise DatasetVerificationError("dataset contains missing intervals")
    if quality.get("invalid_records") != 0 or quality.get("duplicate_timestamps") != 0:
        raise DatasetVerificationError("dataset manifest reports invalid or duplicate source bars")
    if quality.get("out_of_order_timestamps") != 0:
        raise DatasetVerificationError("dataset manifest reports out-of-order source bars")
    return VerifiedDataset(
        str(path.resolve()), dataset_id, instrument.instrument_id,
        actual_start.isoformat(), actual_end.isoformat(), dict(counts), artifact_hashes,
        raw_hashes, len(ingestion.gaps), quality["invalid_records"],
        quality["duplicate_timestamps"], quality["out_of_order_timestamps"],
        quality["zero_volume_intervals"],
    )


def one_minute_path(artifacts: dict[str, Any]) -> Path:
    """Select the only 1m January 2025 partition or fail closed."""
    entry = artifacts.get("1m:2025-01")
    if not isinstance(entry, dict) or not entry.get("path"):
        raise DatasetVerificationError("expected 1m January 2025 artifact is absent")
    return Path(entry["path"])


def deterministic_not_run_id(
    dataset_id: str, strategy_id: str, strategy_configuration_hash: str, reason: str
) -> str:
    """Derive a stable ID for a blocked/not-run experiment record."""
    encoded = json.dumps(
        [dataset_id, strategy_id, strategy_configuration_hash, reason],
        separators=(",", ":"),
    ).encode()
    return "not-run-" + hashlib.sha256(encoded).hexdigest()[:20]
