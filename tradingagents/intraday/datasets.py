"""Immutable, provenance-rich historical dataset validation and persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.intraday.aggregation import aggregate_bars
from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.ingest import file_sha256
from tradingagents.intraday.models import InstrumentMetadata, MarketBar

TRANSFORMATION_VERSION = "tradingagents-intraday-dataset-v1"
DEFAULT_DATA_ROOT = Path.home() / ".local" / "share" / "tradingagents" / "datasets"


@dataclass(frozen=True, slots=True)
class QualityReport:
    records: int
    invalid_records: int
    negative_price_records: int
    duplicate_timestamps: int
    out_of_order_timestamps: int
    missing_intervals: int
    expected_intervals: int
    completeness_ratio: float | None
    zero_volume_intervals: int
    zero_volume_is_confirmed_no_trade: bool
    abnormal_price_jumps: tuple[dict[str, str], ...]
    abnormal_volume_intervals: tuple[str, ...]
    outside_session_records: int


def assess_quality(
    bars: list[MarketBar] | tuple[MarketBar, ...],
    instrument: InstrumentMetadata,
    *,
    expected_start: date,
    expected_end: date,
    price_jump_fraction: Decimal = Decimal("0.25"),
    volume_median_multiple: Decimal = Decimal("10"),
) -> QualityReport:
    """Report gaps and anomalies without sorting, filling, or adjusting source data."""
    if expected_end < expected_start:
        raise ValueError("expected_end must not precede expected_start")
    stamps = [bar.timestamp for bar in bars]
    duplicates = len(stamps) - len(set(stamps))
    out_of_order = sum(stamps[i] < stamps[i - 1] for i in range(1, len(stamps)))
    wrong_ids = sum(bar.instrument_id != instrument.instrument_id for bar in bars)
    negative_prices = sum(
        min(bar.open, bar.high, bar.low, bar.close) < 0 and not instrument.allow_negative_prices
        for bar in bars
    )
    intervals = {bar.base_interval_minutes for bar in bars}
    if len(intervals) > 1:
        raise ValueError("quality assessment requires one base interval")
    interval = next(iter(intervals), 1)
    calendar = SessionCalendar(instrument.session_calendar)
    expected = [
        stamp
        for session in calendar.sessions(expected_start, expected_end)
        for stamp in calendar.expected_bar_starts(session, interval)
    ]
    supplied = set(stamps)
    missing = len(set(expected) - supplied)
    outside = sum(calendar.session_for_bar(stamp, timedelta(minutes=interval)) is None for stamp in stamps)
    zero_volume = sum(bar.volume == 0 for bar in bars)
    close_jumps: list[dict[str, str]] = []
    abnormal_volume: list[str] = []
    chronological = sorted((b for b in bars if b.instrument_id == instrument.instrument_id), key=lambda b: b.timestamp)
    closes = [b.close for b in chronological]
    for idx in range(1, len(chronological)):
        previous, current = closes[idx - 1], closes[idx]
        if previous > 0 and abs(current - previous) / previous >= price_jump_fraction:
            close_jumps.append({"timestamp": chronological[idx].timestamp.isoformat(),
                                "previous_close": str(previous), "close": str(current),
                                "flag": "possible_corporate_action_or_dislocation"})
        window = [bar.volume for bar in chronological[max(0, idx - 20):idx] if bar.volume > 0]
        if len(window) >= 5:
            median = sorted(window)[len(window) // 2]
            if median > 0 and chronological[idx].volume > median * volume_median_multiple:
                abnormal_volume.append(chronological[idx].timestamp.isoformat())
    invalid = wrong_ids + duplicates + out_of_order + outside + negative_prices
    return QualityReport(
        records=len(bars), invalid_records=invalid, negative_price_records=negative_prices,
        duplicate_timestamps=duplicates, out_of_order_timestamps=out_of_order,
        missing_intervals=missing, expected_intervals=len(expected),
        completeness_ratio=(1 - missing / len(expected)) if expected else None,
        zero_volume_intervals=zero_volume, zero_volume_is_confirmed_no_trade=False,
        abnormal_price_jumps=tuple(close_jumps), abnormal_volume_intervals=tuple(abnormal_volume),
        outside_session_records=outside,
    )


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def _atomic_bytes(path: Path, content: bytes, *, immutable: bool = True) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = file_sha256(path)
        incoming = hashlib.sha256(content).hexdigest()
        if immutable and existing != incoming:
            raise FileExistsError(f"immutable dataset artifact already exists with different content: {path}") from None
        return existing
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        if immutable and file_sha256(path) != hashlib.sha256(content).hexdigest():
            raise FileExistsError(
                f"immutable dataset artifact already exists with different content: {path}"
            ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return file_sha256(path)


def _bar_row(bar: MarketBar) -> dict[str, Any]:
    return {
        "timestamp": bar.timestamp, "instrument_id": bar.instrument_id,
        "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
        "close": float(bar.close), "volume": float(bar.volume),
        "source_id": bar.source_id, "source_timestamp": bar.source_timestamp,
        "base_interval_minutes": bar.base_interval_minutes, "quality": bar.quality,
        "exchange_local_timestamp": bar.exchange_local_timestamp,
    }


def persist_dataset(
    bars: list[MarketBar] | tuple[MarketBar, ...],
    instrument: InstrumentMetadata,
    *,
    provider: str,
    source_reference: str,
    requested_start: date,
    requested_end: date,
    raw_payloads: list[tuple[str, bytes]],
    instrument_metadata_version: str,
    instrument_metadata: dict[str, Any] | None = None,
    source_terms_reference: str,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Persist raw payloads, normalized 1m and derived 5/15m Parquet plus manifest.

    Dataset writes are content-immutable: a path collision with different bytes raises.
    The default data root is user-local and outside the source checkout.
    """
    if not bars:
        raise ValueError("cannot persist an empty dataset")
    if any(b.instrument_id != instrument.instrument_id or b.base_interval_minutes != 1 for b in bars):
        raise ValueError("dataset must contain 1-minute bars for the selected instrument")
    ordered = list(bars)
    if [b.timestamp for b in ordered] != sorted(b.timestamp for b in ordered):
        raise ValueError("dataset bars must be chronological")
    if len({b.timestamp for b in ordered}) != len(ordered):
        raise ValueError("dataset contains duplicate timestamps")
    root = Path(data_root or os.environ.get("TRADINGAGENTS_DATA_DIR", DEFAULT_DATA_ROOT))
    first, last = ordered[0].timestamp, ordered[-1].timestamp
    raw_digests = [(name, hashlib.sha256(content).hexdigest()) for name, content in raw_payloads]
    identity = {
        "provider": provider, "instrument": instrument.instrument_id,
        "range": [requested_start.isoformat(), requested_end.isoformat()],
        "raw_sha256": raw_digests, "transform": TRANSFORMATION_VERSION,
    }
    dataset_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    base = root / f"market={_safe(instrument.session_calendar.lower())}" / f"instrument={_safe(instrument.instrument_id)}"
    raw_artifacts = []
    for name, content in raw_payloads:
        raw_path = base / "raw" / f"dataset={dataset_id}" / _safe(Path(name).name)
        digest = _atomic_bytes(raw_path, content)
        raw_artifacts.append({"name": name, "path": str(raw_path), "sha256": digest})
    rows = [_bar_row(bar) for bar in ordered]
    frame = pd.DataFrame(rows)
    quality = assess_quality(ordered, instrument, expected_start=requested_start, expected_end=requested_end)
    artifacts: dict[str, dict[str, str]] = {}
    interval_record_counts = {"1m": 0, "5m": 0, "15m": 0}
    for interval in (1, 5, 15):
        part = frame.copy() if interval == 1 else None
        if interval > 1:
            candles = aggregate_bars(ordered, instrument, interval)
            part = pd.DataFrame([{
                "timestamp": c.timestamp, "end_timestamp": c.end_timestamp,
                "instrument_id": c.instrument_id, "open": float(c.open) if c.open is not None else None,
                "high": float(c.high) if c.high is not None else None,
                "low": float(c.low) if c.low is not None else None,
                "close": float(c.close) if c.close is not None else None,
                "volume": float(c.volume) if c.volume is not None else None,
                "is_valid": c.is_valid, "invalid_reason": c.invalid_reason,
                "source_ids": list(c.source_ids), "interval_minutes": interval,
            } for c in candles])
        if part.empty:
            continue
        interval_record_counts[f"{interval}m"] = len(part)
        part["year"] = pd.to_datetime(part.timestamp, utc=True).dt.year
        part["month"] = pd.to_datetime(part.timestamp, utc=True).dt.month
        part["interval"] = f"{interval}m"
        for (year, month), grouped in part.groupby(["year", "month"], sort=True):
            parquet_path = base / f"year={year}" / f"month={month:02d}" / f"interval={interval}m" / f"transform={TRANSFORMATION_VERSION}" / f"dataset={dataset_id}.parquet"
            buffer = __import__("io").BytesIO()
            grouped.drop(columns=["year", "month", "interval"]).to_parquet(buffer, index=False)
            digest = _atomic_bytes(parquet_path, buffer.getvalue())
            artifacts[f"{interval}m:{year}-{month:02d}"] = {
                "path": str(parquet_path), "sha256": digest, "record_count": len(grouped)
            }
    expected = [
        stamp for session in SessionCalendar(instrument.session_calendar).sessions(requested_start, requested_end)
        for stamp in SessionCalendar(instrument.session_calendar).expected_bar_starts(session, 1)
    ]
    manifest = {
        "dataset_id": dataset_id, "market": instrument.session_calendar,
        "instrument_id": instrument.instrument_id, "symbol": instrument.symbol,
        "provider": provider, "source_reference": source_reference,
        "interval": "1m", "requested_date_range": [requested_start.isoformat(), requested_end.isoformat()],
        "actual_available_range_utc": [first.isoformat(), last.isoformat()],
        "retrieved_at_utc": datetime.now(UTC).isoformat(), "record_count": len(ordered),
        "interval_record_counts": interval_record_counts,
        "invalid_record_count": quality.invalid_records, "missing_interval_count": quality.missing_intervals,
        "expected_interval_count": quality.expected_intervals, "completeness_ratio": quality.completeness_ratio,
        "quality_report": asdict(quality), "raw_payloads": raw_artifacts,
        "artifacts": artifacts, "instrument_metadata_version": instrument_metadata_version,
        "instrument_metadata": instrument_metadata,
        "calendar_provider_version": SessionCalendar(instrument.session_calendar).provenance,
        "transformation_version": TRANSFORMATION_VERSION,
        "source_terms_reference": source_terms_reference,
        "corporate_action_policy": "Flag discontinuities; preserve unadjusted source prices; no automatic adjustment.",
        "historical_universe_label": "limited research universe; current instrument availability is not historical index membership",
        "expected_intervals_outside_actual_data_range": len(set(expected) - {b.timestamp for b in ordered}),
    }
    manifest_path = base / "manifests" / f"dataset={dataset_id}.json"
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("existing immutable dataset manifest is invalid") from exc
        if previous_manifest.get("dataset_id") != dataset_id:
            raise ValueError("existing manifest does not match the requested dataset identity")
        return previous_manifest
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    _atomic_bytes(manifest_path, manifest_bytes)
    return json.loads(manifest_bytes)


def trailing_trading_sessions_end(start: date, session_count: int, calendar_name: str = "XNSE") -> date:
    """Find the end of N exchange sessions, supporting the configurable pilot path."""
    if session_count < 1:
        raise ValueError("session_count must be positive")
    calendar = SessionCalendar(calendar_name)
    end = start
    while len(calendar.sessions(start, end)) < session_count:
        end += timedelta(days=max(7, session_count // 4))
    sessions = calendar.sessions(start, end)
    return sessions[session_count - 1].session_date
