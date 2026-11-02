"""Fail-closed experiment preflight tests; fixtures are not trading results."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.intraday import experiment_validation as validation
from tradingagents.intraday.models import InstrumentMetadata


def _instrument():
    return InstrumentMetadata(
        instrument_id="BINANCE_SPOT:BTCUSDT", symbol="BTCUSDT", venue="BINANCE_SPOT",
        asset_class="crypto_spot", base_currency="BTC", quote_currency="USDT",
        price_precision=8, tick_size=Decimal("0.00000001"),
        quantity_increment=Decimal("0.00000001"), exchange_timezone="UTC",
        session_calendar="24/7", data_source="binance-public-data", provenance="test fixture",
    )


def _valid_fixture(tmp_path, monkeypatch):
    stamp = datetime(2025, 1, 1, tzinfo=UTC)
    archive = b"raw archive fixture"
    archive_hash = hashlib.sha256(archive).hexdigest()
    sidecar = f"{archive_hash} BTCUSDT-1m-2025-01.zip\n".encode()
    archive_path = tmp_path / "BTCUSDT-1m-2025-01.zip"
    sidecar_path = tmp_path / "BTCUSDT-1m-2025-01.zip.CHECKSUM"
    archive_path.write_bytes(archive)
    sidecar_path.write_bytes(sidecar)
    raw = [
        {"name": archive_path.name, "path": str(archive_path), "sha256": archive_hash},
        {"name": sidecar_path.name, "path": str(sidecar_path), "sha256": hashlib.sha256(sidecar).hexdigest()},
    ]
    artifacts = {}
    for interval, count in (("1m", 1), ("5m", 1), ("15m", 1)):
        key = f"{interval}:2025-01"
        artifact_path = tmp_path / f"{interval}.parquet"
        frame = pd.DataFrame({
            "timestamp": [stamp], "is_valid": [True],
        })
        frame.to_parquet(artifact_path, index=False)
        artifacts[key] = {
            "path": str(artifact_path), "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "record_count": count,
        }
    manifest = {
        "dataset_id": "fixture-dataset", "instrument_id": "BINANCE_SPOT:BTCUSDT",
        "interval": "1m", "transformation_version": "tradingagents-intraday-dataset-v1",
        "raw_payloads": raw, "artifacts": artifacts,
        "interval_record_counts": {"1m": 1, "5m": 1, "15m": 1},
        "actual_available_range_utc": [stamp.isoformat(), stamp.isoformat()],
        "record_count": 1,
        "quality_report": {
            "missing_intervals": 0, "invalid_records": 0, "duplicate_timestamps": 0,
            "out_of_order_timestamps": 0, "zero_volume_intervals": 0,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        validation, "read_market_data",
        lambda *_args, **_kwargs: type("Ingest", (), {"bars": (object(),), "gaps": ()})(),
    )
    return manifest_path, archive_path


def test_dataset_preflight_verifies_raw_normalized_and_derived_hashes(tmp_path, monkeypatch):
    manifest_path, _ = _valid_fixture(tmp_path, monkeypatch)
    result = validation.verify_dataset_manifest(
        manifest_path, _instrument(), expected_dataset_id="fixture-dataset",
        expected_counts={"1m": 1, "5m": 1, "15m": 1},
        expected_actual_range_utc=("2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    )
    assert result.dataset_id == "fixture-dataset"
    assert result.record_counts == {"1m": 1, "5m": 1, "15m": 1}
    assert result.missing_intervals == result.invalid_records == result.duplicate_timestamps == 0


def test_dataset_preflight_aborts_on_source_hash_mismatch(tmp_path, monkeypatch):
    manifest_path, archive_path = _valid_fixture(tmp_path, monkeypatch)
    archive_path.write_bytes(b"changed archive")
    with pytest.raises(validation.DatasetVerificationError, match="raw artifact checksum failed"):
        validation.verify_dataset_manifest(manifest_path, _instrument())


def test_dataset_preflight_aborts_on_requested_count_mismatch(tmp_path, monkeypatch):
    manifest_path, _ = _valid_fixture(tmp_path, monkeypatch)
    with pytest.raises(validation.DatasetVerificationError, match="counts do not match"):
        validation.verify_dataset_manifest(
            manifest_path, _instrument(), expected_counts={"1m": 44640, "5m": 8928, "15m": 2976}
        )


def test_not_run_ids_are_reproducible_and_distinct_by_strategy():
    first = validation.deterministic_not_run_id("dataset", "vwap", "config", "anchor missing")
    again = validation.deterministic_not_run_id("dataset", "vwap", "config", "anchor missing")
    other = validation.deterministic_not_run_id("dataset", "orb", "config", "anchor missing")
    assert first == again and first != other
