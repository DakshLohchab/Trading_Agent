"""Validated contracts used by the intraday historical-data boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AssetClass = Literal["equity", "crypto_spot"]


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be {'positive and ' if positive else ''}finite")
    return result


@dataclass(frozen=True, slots=True)
class InstrumentMetadata:
    instrument_id: str
    symbol: str
    venue: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    price_precision: int
    tick_size: Decimal
    quantity_increment: Decimal
    exchange_timezone: str
    session_calendar: str
    data_source: str
    provenance: str
    min_order_quantity: Decimal | None = None
    min_order_notional: Decimal | None = None
    allow_negative_prices: bool = False

    def __post_init__(self) -> None:
        for name in (
            "instrument_id",
            "symbol",
            "venue",
            "base_currency",
            "quote_currency",
            "session_calendar",
            "data_source",
            "provenance",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} cannot be blank")
        if self.instrument_id == self.symbol:
            raise ValueError("instrument_id must be venue-qualified, not a ticker alone")
        if self.asset_class not in ("equity", "crypto_spot"):
            raise ValueError(f"unsupported asset_class: {self.asset_class}")
        if self.price_precision < 0:
            raise ValueError("price_precision cannot be negative")
        object.__setattr__(self, "tick_size", _decimal(self.tick_size, "tick_size", positive=True))
        object.__setattr__(
            self,
            "quantity_increment",
            _decimal(self.quantity_increment, "quantity_increment", positive=True),
        )
        if self.tick_size.normalize().as_tuple().exponent < -self.price_precision:
            raise ValueError("tick_size requires more decimals than price_precision")
        for name in ("min_order_quantity", "min_order_notional"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name, positive=True))
        try:
            ZoneInfo(self.exchange_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown exchange timezone: {self.exchange_timezone}") from exc
        if self.asset_class == "crypto_spot" and self.session_calendar != "24/7":
            raise ValueError("crypto spot instruments must use the 24/7 calendar")
        expected_timezones = {
            "NSE": {"Asia/Kolkata", "Asia/Calcutta"},
            "BSE": {"Asia/Kolkata", "Asia/Calcutta"},
            "XNSE": {"Asia/Kolkata", "Asia/Calcutta"},
            "NYSE": {"America/New_York", "US/Eastern"},
            "NASDAQ": {"America/New_York", "US/Eastern"},
            "XNYS": {"America/New_York", "US/Eastern"},
            "24/7": {"UTC", "Etc/UTC"},
        }.get(self.session_calendar.upper())
        if expected_timezones and self.exchange_timezone not in expected_timezones:
            raise ValueError("exchange_timezone does not match the selected session calendar")


@dataclass(frozen=True, slots=True)
class CurrencyConversionMetadata:
    """Point-in-time FX conversion quote kept separate from instrument identity."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    observed_at: datetime
    source_id: str

    def __post_init__(self) -> None:
        if (
            not self.base_currency.strip()
            or not self.quote_currency.strip()
            or self.base_currency == self.quote_currency
        ):
            raise ValueError("conversion currencies must be non-empty and distinct")
        object.__setattr__(self, "rate", _decimal(self.rate, "rate", positive=True))
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(ZoneInfo("UTC")))
        if not self.source_id.strip():
            raise ValueError("source_id cannot be blank")


@dataclass(frozen=True, slots=True)
class MarketBar:
    """One interval-start OHLCV bar; `timestamp` is always normalized to UTC."""

    timestamp: datetime
    instrument_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_id: str
    source_timestamp: str
    base_interval_minutes: int = 1
    quality: Literal["observed", "confirmed_no_trade"] = "observed"
    exchange_local_timestamp: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(ZoneInfo("UTC")))
        if not self.instrument_id.strip() or not self.source_id.strip():
            raise ValueError("instrument_id and source_id cannot be blank")
        if not self.source_timestamp.strip():
            raise ValueError("source_timestamp cannot be blank")
        if self.base_interval_minutes <= 0:
            raise ValueError("base_interval_minutes must be positive")
        values = {
            name: _decimal(getattr(self, name), name)
            for name in ("open", "high", "low", "close", "volume")
        }
        if values["volume"] < 0:
            raise ValueError("volume cannot be negative")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError("high must be >= open, low, and close")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError("low must be <= open, high, and close")
        if self.quality not in ("observed", "confirmed_no_trade"):
            raise ValueError("unknown bar quality")
        if self.quality == "confirmed_no_trade" and (
            values["volume"] != 0 or len({values[n] for n in ("open", "high", "low", "close")}) != 1
        ):
            raise ValueError("confirmed_no_trade requires zero volume and a single flat price")
        for name, value in values.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class DataGap:
    instrument_id: str
    start: datetime
    end: datetime
    reason: str = "missing_interval"


@dataclass(frozen=True, slots=True)
class AggregatedCandle:
    timestamp: datetime
    end_timestamp: datetime
    instrument_id: str
    interval_minutes: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    is_valid: bool
    invalid_reason: str | None = None
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("timestamp", "end_timestamp"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(ZoneInfo("UTC")))
        if self.end_timestamp <= self.timestamp:
            raise ValueError("end_timestamp must be after timestamp")
        if self.interval_minutes not in (5, 15):
            raise ValueError("aggregated candle interval must be 5 or 15 minutes")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be blank")
        names = ("open", "high", "low", "close", "volume")
        if self.is_valid and any(getattr(self, name) is None for name in names):
            raise ValueError("valid candle values cannot be missing")
        if self.is_valid:
            for name in names:
                object.__setattr__(self, name, _decimal(getattr(self, name), name))
            if (
                self.volume < 0
                or self.high < max(self.open, self.low, self.close)
                or self.low > min(self.open, self.high, self.close)
            ):
                raise ValueError("valid candle OHLCV values are inconsistent")


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    sequence: int
    event_timestamp: datetime
    available_at: datetime
    bar: MarketBar | AggregatedCandle
    replay_id: str
