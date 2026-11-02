"""Fail-closed parser for public Binance Spot symbol-rule snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tradingagents.intraday.models import InstrumentMetadata, MarketBar

PARSER_VERSION = "binance-spot-exchangeinfo-v1"
D = Decimal


class ExchangeFilterError(ValueError):
    """A Binance Spot snapshot is missing or contradicts required market rules."""


def _decimal(value: object, label: str, *, zero_allowed: bool = False) -> Decimal:
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExchangeFilterError(f"{label} is not a decimal") from exc
    if not result.is_finite() or result < 0 or (not zero_allowed and result == 0):
        raise ExchangeFilterError(f"{label} is outside its allowed range")
    return result


@dataclass(frozen=True, slots=True)
class BinanceSpotFilters:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    base_asset_precision: int
    quote_asset_precision: int
    spot_enabled: bool
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    lot_step_size: Decimal
    lot_min_quantity: Decimal
    lot_max_quantity: Decimal
    market_step_size: Decimal
    market_min_quantity: Decimal
    market_max_quantity: Decimal
    min_notional: Decimal
    max_notional: Decimal | None
    min_notional_applies_to_market: bool
    percent_price_avg_minutes: int | None
    bid_multiplier_down: Decimal | None
    bid_multiplier_up: Decimal | None
    ask_multiplier_down: Decimal | None
    ask_multiplier_up: Decimal | None
    retrieved_at_utc: datetime
    exchange_server_time_utc: datetime | None
    source_endpoint: str
    raw_response_sha256: str
    parser_version: str = PARSER_VERSION

    def __post_init__(self) -> None:
        if self.symbol != "BTCUSDT" or self.base_asset != "BTC" or self.quote_asset != "USDT":
            raise ExchangeFilterError("snapshot identity must be BTCUSDT with BTC/USDT assets")
        if self.status != "TRADING" or not self.spot_enabled:
            raise ExchangeFilterError("BTCUSDT is not currently eligible for Spot trading")
        if self.base_asset_precision <= 0 or self.quote_asset_precision <= 0:
            raise ExchangeFilterError("asset precision metadata is missing or invalid")
        percent_values = (
            self.bid_multiplier_down,
            self.bid_multiplier_up,
            self.ask_multiplier_down,
            self.ask_multiplier_up,
        )
        if self.percent_price_avg_minutes is not None and (
            self.percent_price_avg_minutes <= 0
            or any(value is None or value <= 0 for value in percent_values)
        ):
            raise ExchangeFilterError("PERCENT_PRICE_BY_SIDE filter is incomplete")
        if self.retrieved_at_utc.tzinfo is None or self.retrieved_at_utc.utcoffset() is None:
            raise ExchangeFilterError("retrieval timestamp must be timezone-aware")
        if len(self.raw_response_sha256) != 64:
            raise ExchangeFilterError("raw response SHA-256 is malformed")
        if not self.source_endpoint.startswith("https://api.binance.com/api/v3/exchangeInfo?"):
            raise ExchangeFilterError("snapshot source endpoint is not Binance Spot exchangeInfo")

    def as_record(self) -> dict[str, str | bool | None]:
        record = asdict(self)
        return {
            key: value.isoformat() if isinstance(value, datetime)
            else str(value) if isinstance(value, Decimal)
            else value
            for key, value in record.items()
        }


def parse_binance_spot_exchange_info(
    raw_response: bytes,
    *,
    retrieved_at: datetime,
    source_endpoint: str = "https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
) -> BinanceSpotFilters:
    """Parse current public Spot rules; never supplies defaults for missing filters."""
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ExchangeFilterError("retrieval timestamp must include a UTC offset")
    try:
        payload = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExchangeFilterError("exchangeInfo response is not valid JSON") from exc
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list) or len(symbols) != 1 or not isinstance(symbols[0], dict):
        raise ExchangeFilterError("exchangeInfo response must contain exactly BTCUSDT")
    symbol = symbols[0]
    if symbol.get("symbol") != "BTCUSDT":
        raise ExchangeFilterError("exchangeInfo returned a different symbol")
    permission_sets = symbol.get("permissionSets") or []
    permissions = symbol.get("permissions") or []
    spot_enabled = "SPOT" in permissions or any("SPOT" in group for group in permission_sets)
    raw_filters = symbol.get("filters")
    if not isinstance(raw_filters, list):
        raise ExchangeFilterError("symbol filters are missing")
    filters = {}
    for item in raw_filters:
        if not isinstance(item, dict) or not isinstance(item.get("filterType"), str):
            raise ExchangeFilterError("malformed symbol filter")
        filter_type = item["filterType"]
        if filter_type in filters:
            raise ExchangeFilterError(f"duplicate {filter_type} filter")
        filters[filter_type] = item
    try:
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market_lot = filters["MARKET_LOT_SIZE"]
    except KeyError as exc:
        raise ExchangeFilterError(f"required {exc.args[0]} filter is missing") from exc
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
    if notional is None:
        raise ExchangeFilterError("NOTIONAL or MIN_NOTIONAL filter is required")
    retrieved_utc = retrieved_at.astimezone(UTC)
    server_time = payload.get("serverTime")
    server_time_utc = (
        datetime.fromtimestamp(int(server_time) / 1000, UTC) if server_time is not None else None
    )
    min_notional = _decimal(notional.get("minNotional"), "minNotional")
    max_notional_value = notional.get("maxNotional")
    max_notional = _decimal(max_notional_value, "maxNotional") if max_notional_value else None
    percent_price = filters.get("PERCENT_PRICE_BY_SIDE")
    return BinanceSpotFilters(
        symbol=symbol["symbol"],
        status=symbol.get("status", ""),
        base_asset=symbol.get("baseAsset", ""),
        quote_asset=symbol.get("quoteAsset", ""),
        base_asset_precision=int(symbol.get("baseAssetPrecision", -1)),
        quote_asset_precision=int(symbol.get("quoteAssetPrecision", -1)),
        spot_enabled=spot_enabled,
        tick_size=_decimal(price.get("tickSize"), "tickSize"),
        min_price=_decimal(price.get("minPrice"), "minPrice", zero_allowed=True),
        max_price=_decimal(price.get("maxPrice"), "maxPrice", zero_allowed=True),
        lot_step_size=_decimal(lot.get("stepSize"), "LOT_SIZE.stepSize"),
        lot_min_quantity=_decimal(lot.get("minQty"), "LOT_SIZE.minQty"),
        lot_max_quantity=_decimal(lot.get("maxQty"), "LOT_SIZE.maxQty"),
        market_step_size=_decimal(market_lot.get("stepSize"), "MARKET_LOT_SIZE.stepSize", zero_allowed=True),
        market_min_quantity=_decimal(market_lot.get("minQty"), "MARKET_LOT_SIZE.minQty", zero_allowed=True),
        market_max_quantity=_decimal(market_lot.get("maxQty"), "MARKET_LOT_SIZE.maxQty"),
        min_notional=min_notional,
        max_notional=max_notional,
        min_notional_applies_to_market=bool(
            notional.get("applyMinToMarket", notional.get("applyToMarket", False))
        ),
        percent_price_avg_minutes=(
            int(percent_price["avgPriceMins"]) if percent_price is not None else None
        ),
        bid_multiplier_down=(
            _decimal(percent_price.get("bidMultiplierDown"), "bidMultiplierDown")
            if percent_price is not None else None
        ),
        bid_multiplier_up=(
            _decimal(percent_price.get("bidMultiplierUp"), "bidMultiplierUp")
            if percent_price is not None else None
        ),
        ask_multiplier_down=(
            _decimal(percent_price.get("askMultiplierDown"), "askMultiplierDown")
            if percent_price is not None else None
        ),
        ask_multiplier_up=(
            _decimal(percent_price.get("askMultiplierUp"), "askMultiplierUp")
            if percent_price is not None else None
        ),
        retrieved_at_utc=retrieved_utc,
        exchange_server_time_utc=server_time_utc,
        source_endpoint=source_endpoint,
        raw_response_sha256=hashlib.sha256(raw_response).hexdigest(),
    )


def write_filter_snapshot(raw_path: str | Path, record_path: str | Path, *, retrieved_at: datetime) -> BinanceSpotFilters:
    """Validate a retained raw response and write its normalized provenance record."""
    raw = Path(raw_path).read_bytes()
    filters = parse_binance_spot_exchange_info(raw, retrieved_at=retrieved_at)
    record = {
        "snapshot_kind": "current_exchange_rules_not_historical_rules",
        "filters": filters.as_record(),
        "raw_response_path": str(Path(raw_path).resolve()),
        "raw_response_bytes": len(raw),
    }
    Path(record_path).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return filters


def canonicalize_binance_spot_data(
    instrument: InstrumentMetadata,
    bars: list[MarketBar] | tuple[MarketBar, ...],
) -> tuple[InstrumentMetadata, tuple[MarketBar, ...]]:
    """Map the Phase 4B manifest alias to Nautilus' explicit SYMBOL.VENUE form."""
    if (
        instrument.instrument_id != "BINANCE_SPOT:BTCUSDT"
        or instrument.symbol != "BTCUSDT"
        or instrument.venue != "BINANCE_SPOT"
        or instrument.asset_class != "crypto_spot"
        or instrument.base_currency != "BTC"
        or instrument.quote_currency != "USDT"
    ):
        raise ExchangeFilterError("only the known BTCUSDT Phase 4B identity alias can be mapped")
    canonical_id = "BTCUSDT.BINANCE_SPOT"
    mapped_instrument = replace(
        instrument,
        instrument_id=canonical_id,
        provenance=instrument.provenance + "; canonical alias BINANCE_SPOT:BTCUSDT -> BTCUSDT.BINANCE_SPOT",
    )
    mapped_bars = []
    for bar in bars:
        if bar.instrument_id != instrument.instrument_id:
            raise ExchangeFilterError("source bar identity does not match the manifest instrument")
        mapped_bars.append(replace(bar, instrument_id=canonical_id))
    return mapped_instrument, tuple(mapped_bars)
