"""Session-aligned, deterministic aggregation from one-minute OHLCV bars."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from tradingagents.intraday.calendars import SessionCalendar, TradingSession
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata, MarketBar


def aggregate_bars(
    bars: list[MarketBar] | tuple[MarketBar, ...],
    instrument: InstrumentMetadata,
    interval_minutes: int,
    *,
    calendar: SessionCalendar | None = None,
) -> list[AggregatedCandle]:
    """Aggregate 1m input to 5m/15m candles, retaining invalid partial buckets."""
    if interval_minutes not in (5, 15):
        raise ValueError("only 5-minute and 15-minute aggregation is supported")
    if not bars:
        return []
    if any(
        b.instrument_id != instrument.instrument_id or b.base_interval_minutes != 1 for b in bars
    ):
        raise ValueError("aggregation requires 1-minute bars for the selected instrument")
    stamps = [b.timestamp for b in bars]
    if stamps != sorted(stamps):
        raise ValueError("aggregation input must be chronological")
    if len(set(stamps)) != len(stamps):
        raise ValueError("duplicate source bars cannot be aggregated")
    calendar = calendar or SessionCalendar(instrument.session_calendar)
    bar_map = {bar.timestamp: bar for bar in bars}
    sessions: list[TradingSession] = []
    for bar in bars:
        session = calendar.session_for_bar(bar.timestamp)
        if session is None:
            if instrument.asset_class == "equity":
                raise ValueError(
                    f"source bar outside {instrument.session_calendar} session: {bar.timestamp}"
                )
            continue
        if session not in sessions:
            sessions.append(session)
    windows: dict[tuple[datetime, datetime], list[datetime]] = {}
    for session in sessions:
        source_starts = calendar.expected_bar_starts(session, 1)
        for offset in range(0, len(source_starts), interval_minutes):
            constituent_starts = source_starts[offset : offset + interval_minutes]
            if not constituent_starts:
                continue
            bucket_start = constituent_starts[0]
            bucket_end = bucket_start + timedelta(minutes=interval_minutes)
            # Include fully missing buckets between observed bounds as invalid.
            if constituent_starts[0] <= max(stamps) and constituent_starts[-1] >= min(stamps):
                windows[(bucket_start, bucket_end)] = constituent_starts
    output: list[AggregatedCandle] = []
    earliest = min(stamps)
    latest_available = max(stamps) + timedelta(minutes=1)
    for (start, end), expected in sorted(windows.items()):
        if end > latest_available or end <= earliest:
            continue
        actual = [bar_map[t] for t in expected if t in bar_map]
        missing = [t for t in expected if t not in bar_map]
        valid = not missing and len(expected) == interval_minutes
        reason = (
            None
            if valid
            else ("missing_source_intervals" if missing else "incomplete_session_bucket")
        )
        output.append(
            AggregatedCandle(
                timestamp=start,
                end_timestamp=end,
                instrument_id=instrument.instrument_id,
                interval_minutes=interval_minutes,
                open=actual[0].open if actual else None,
                high=max((b.high for b in actual), default=None),
                low=min((b.low for b in actual), default=None),
                close=actual[-1].close if actual else None,
                volume=sum((b.volume for b in actual), start=Decimal(0)),
                is_valid=valid,
                invalid_reason=reason,
                source_ids=tuple(b.source_id for b in actual),
            )
        )
    return output
