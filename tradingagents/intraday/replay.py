"""Checksum-addressed replay with SQLite metadata and completion-time events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.models import (
    AggregatedCandle,
    InstrumentMetadata,
    MarketBar,
    ReplayEvent,
)


@dataclass(frozen=True, slots=True)
class ReplayMetadata:
    replay_id: str
    instruments: tuple[str, ...]
    data_sources: tuple[str, ...]
    data_checksums: tuple[tuple[str, str], ...]
    start: str | None
    end: str | None
    calendar_provenance: tuple[str, ...]
    candle_interval_minutes: int
    speed: float
    software_version: str


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("replay boundaries must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class ReplayMetadataStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS replay_metadata (replay_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL)"
            )

    def put(self, metadata: ReplayMetadata) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO replay_metadata(replay_id, metadata_json) VALUES (?, ?)",
                (
                    metadata.replay_id,
                    json.dumps(
                        metadata.__dict__
                        if hasattr(metadata, "__dict__")
                        else {
                            field: getattr(metadata, field)
                            for field in metadata.__dataclass_fields__
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def get(self, replay_id: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT metadata_json FROM replay_metadata WHERE replay_id = ?", (replay_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None


class HistoricalReplay:
    """Replay completed bars in stable UTC order; start/end filter availability time."""

    def __init__(
        self,
        instruments: dict[str, InstrumentMetadata],
        bars: Iterable[MarketBar | AggregatedCandle],
        *,
        data_checksums: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        instrument_universe: Iterable[str] | None = None,
        speed: float = 0,
        metadata_store: ReplayMetadataStore | None = None,
        candle_interval_minutes: int = 1,
    ):
        if speed < 0:
            raise ValueError("replay speed must be >= 0")
        self.start = _iso_utc(start)
        self.end = _iso_utc(end)
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must not follow end")
        selected = set(instrument_universe) if instrument_universe is not None else set(instruments)
        if not selected:
            raise ValueError("replay instrument universe cannot be empty")
        for key, instrument in instruments.items():
            if key != instrument.instrument_id:
                raise ValueError(f"instrument metadata key mismatch for {key!r}")
        unknown = selected - instruments.keys()
        if unknown:
            raise ValueError(f"unknown replay instruments: {sorted(unknown)}")
        if candle_interval_minutes not in (1, 5, 15):
            raise ValueError("candle_interval_minutes must be 1, 5, or 15")
        self.speed = speed
        self.metadata_store = metadata_store
        self.instrument_metadata = {key: instruments[key] for key in selected}
        self.data_checksums = dict(sorted((data_checksums or {}).items()))
        self.bars = tuple(bars)
        self.candle_interval_minutes = candle_interval_minutes
        self._events = self._prepare_events()
        try:
            from tradingagents import __version__ as software_version
        except Exception:
            software_version = "unknown"
        sources = tuple(
            sorted({i.data_source + ";" + i.provenance for i in self.instrument_metadata.values()})
        )
        calendars = tuple(
            sorted(
                {
                    SessionCalendar(i.session_calendar).provenance
                    for i in self.instrument_metadata.values()
                }
            )
        )
        payload = {
            "instruments": sorted(selected),
            "data_checksums": self.data_checksums,
            "start": self.start,
            "end": self.end,
            "calendars": calendars,
            "candle_interval_minutes": candle_interval_minutes,
            "speed": speed,
            "software_version": software_version,
            "events": [
                (
                    e.event_timestamp.isoformat(),
                    e.bar.instrument_id,
                    e.bar.timestamp.isoformat(),
                    str(e.bar.open),
                    str(e.bar.high),
                    str(e.bar.low),
                    str(e.bar.close),
                    str(e.bar.volume),
                    getattr(e.bar, "source_id", getattr(e.bar, "source_ids", ())),
                    getattr(e.bar, "source_timestamp", ""),
                    getattr(e.bar, "quality", ""),
                )
                for e in self._events
            ],
        }
        replay_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.metadata = ReplayMetadata(
            replay_id,
            tuple(sorted(selected)),
            sources,
            tuple(self.data_checksums.items()),
            self.start,
            self.end,
            calendars,
            candle_interval_minutes,
            speed,
            software_version,
        )
        self.events = tuple(
            ReplayEvent(e.sequence, e.event_timestamp, e.available_at, e.bar, replay_id)
            for e in self._events
        )
        if metadata_store:
            metadata_store.put(self.metadata)

    def _prepare_events(self) -> list[ReplayEvent]:
        selected = set(self.instrument_metadata)
        bars = [bar for bar in self.bars if bar.instrument_id in selected]
        keys = []
        for bar in bars:
            duration = timedelta(
                minutes=bar.interval_minutes
                if isinstance(bar, AggregatedCandle)
                else bar.base_interval_minutes
            )
            available = (
                bar.end_timestamp if isinstance(bar, AggregatedCandle) else bar.timestamp + duration
            )
            if isinstance(bar, AggregatedCandle) and not bar.is_valid:
                continue
            if self.start and available.isoformat() < self.start:
                continue
            if self.end and available.isoformat() > self.end:
                continue
            keys.append((available, bar.instrument_id, bar.timestamp, bar))
        keys.sort(key=lambda item: (item[0], item[1], item[2]))
        for a, b in zip(keys, keys[1:], strict=False):
            if a[:3] == b[:3]:
                raise ValueError(f"duplicate replay event for {b[1]} at {b[0].isoformat()}")
        return [ReplayEvent(i, item[0], item[0], item[3], "") for i, item in enumerate(keys)]

    def iter_events(self, callback: Callable[[ReplayEvent], None] | None = None):
        previous: datetime | None = None
        for event in self.events:
            if self.speed and previous is not None:
                time.sleep(
                    max(0.0, (event.event_timestamp - previous).total_seconds() / self.speed)
                )
            if callback:
                callback(event)
            yield event
            previous = event.event_timestamp
