"""Local CSV/Parquet ingestion with strict schema, timestamp, and gap checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.models import DataGap, InstrumentMetadata, MarketBar

REQUIRED_COLUMNS = {
    "timestamp",
    "instrument_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source_id",
    "source_timestamp",
    "base_interval_minutes",
}


@dataclass(frozen=True, slots=True)
class IngestionResult:
    bars: tuple[MarketBar, ...]
    gaps: tuple[DataGap, ...]
    file_sha256: str
    source_path: str


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamp(value: object, timezone_name: str) -> tuple[datetime, str]:
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp {raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        local_tz = ZoneInfo(timezone_name)
        candidate = parsed.replace(tzinfo=local_tz)
        # Reject ambiguous/nonexistent local wall-clock times instead of guessing.
        roundtrip = candidate.astimezone(ZoneInfo("UTC")).astimezone(local_tz).replace(tzinfo=None)
        alternate = parsed.replace(tzinfo=local_tz, fold=1)
        if roundtrip != parsed or alternate.utcoffset() != candidate.utcoffset():
            raise ValueError(
                f"ambiguous or nonexistent local timestamp {raw!r}; provide a UTC offset"
            )
        return candidate.astimezone(ZoneInfo("UTC")), raw
    return parsed.astimezone(ZoneInfo("UTC")), raw


def read_market_data(
    path: str | Path,
    instrument: InstrumentMetadata,
    *,
    allow_out_of_session: bool = False,
    detect_gaps: bool = True,
) -> IngestionResult:
    """Load a canonical CSV or Parquet file without sorting or filling source rows."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(source)
    elif suffix in (".parquet", ".pq"):
        try:
            frame = pd.read_parquet(source)
        except ImportError as exc:
            raise RuntimeError(
                "Parquet ingestion requires an installed pandas parquet engine"
            ) from exc
    else:
        raise ValueError("supported data formats are CSV and Parquet")
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
    if frame.empty:
        raise ValueError("input dataset is empty")
    bars: list[MarketBar] = []
    previous: datetime | None = None
    seen: set[datetime] = set()
    calendar = SessionCalendar(instrument.session_calendar)
    for row_number, row in frame.iterrows():
        if str(row.instrument_id) != instrument.instrument_id:
            raise ValueError(
                f"row {row_number + 2}: instrument_id does not match selected metadata"
            )
        if pd.isna(row.source_timestamp) or not str(row.source_timestamp).strip():
            raise ValueError(f"row {row_number + 2}: source_timestamp cannot be blank")
        stamp, local_text = _timestamp(row.timestamp, instrument.exchange_timezone)
        if stamp in seen:
            raise ValueError(f"duplicate timestamp {stamp.isoformat()}")
        if previous is not None and stamp < previous:
            raise ValueError(f"out-of-order timestamp at row {row_number + 2}")
        seen.add(stamp)
        previous = stamp
        interval = int(row.base_interval_minutes)
        duration = timedelta(minutes=interval)
        if stamp.second or stamp.microsecond or stamp.minute % interval:
            raise ValueError(f"row {row_number + 2}: timestamp is not aligned to interval")
        if (
            not instrument.allow_negative_prices
            and min(float(row.open), float(row.high), float(row.low), float(row.close)) < 0
        ):
            raise ValueError(
                f"row {row_number + 2}: negative prices are invalid for this instrument"
            )
        bar = MarketBar(
            timestamp=stamp,
            instrument_id=instrument.instrument_id,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            source_id=str(row.source_id),
            source_timestamp=str(row.source_timestamp),
            base_interval_minutes=interval,
            quality=str(row["quality"]) if "quality" in frame.columns else "observed",
            exchange_local_timestamp=local_text,
        )
        if (
            instrument.asset_class == "equity"
            and not allow_out_of_session
            and calendar.session_for_bar(stamp, duration) is None
        ):
            raise ValueError(
                f"row {row_number + 2}: bar is outside {instrument.session_calendar} session"
            )
        bars.append(bar)
    intervals = {bar.base_interval_minutes for bar in bars}
    if len(intervals) != 1:
        raise ValueError("inconsistent base intervals in one dataset")
    gaps = tuple(detect_missing_intervals(bars, instrument, calendar)) if detect_gaps else ()
    return IngestionResult(tuple(bars), gaps, file_sha256(source), str(source.resolve()))


def detect_missing_intervals(
    bars: list[MarketBar] | tuple[MarketBar, ...],
    instrument: InstrumentMetadata,
    calendar: SessionCalendar | None = None,
) -> list[DataGap]:
    """Return missing scheduled bars between the first and last supplied bar."""
    if not bars:
        return []
    calendar = calendar or SessionCalendar(instrument.session_calendar)
    interval = bars[0].base_interval_minutes
    supplied = {bar.timestamp for bar in bars}
    first, last = min(supplied), max(supplied)
    start_date = first.astimezone(ZoneInfo(instrument.exchange_timezone)).date()
    end_date = last.astimezone(ZoneInfo(instrument.exchange_timezone)).date()
    expected = []
    for session in calendar.sessions(start_date, end_date):
        expected.extend(
            t for t in calendar.expected_bar_starts(session, interval) if first <= t <= last
        )
    return [
        DataGap(instrument.instrument_id, stamp, stamp + timedelta(minutes=interval))
        for stamp in expected
        if stamp not in supplied
    ]
