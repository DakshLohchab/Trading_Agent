"""Deterministic, completed-candle indicators for the intraday strategy boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata

D = Decimal
INDICATOR_ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    ema_fast: int = 9
    ema_trend: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_period: int = 14
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_deviations: Decimal = D("2")
    relative_volume_lookback: int = 20
    ema_slope_lookback: int = 1

    def __post_init__(self) -> None:
        deviations = D(str(self.bollinger_deviations))
        if not deviations.is_finite():
            raise ValueError("Bollinger deviations must be finite")
        object.__setattr__(self, "bollinger_deviations", deviations)
        periods = (
            self.ema_fast,
            self.ema_trend,
            self.ema_slow,
            self.rsi_period,
            self.macd_fast,
            self.macd_slow,
            self.macd_signal,
            self.adx_period,
            self.atr_period,
            self.bollinger_period,
            self.relative_volume_lookback,
            self.ema_slope_lookback,
        )
        if any(period <= 0 for period in periods):
            raise ValueError("indicator periods must be positive")
        if self.ema_fast >= self.ema_trend or self.ema_trend >= self.ema_slow:
            raise ValueError("EMA periods must satisfy fast < trend < slow")
        if self.macd_fast >= self.macd_slow or self.bollinger_deviations <= 0:
            raise ValueError("invalid MACD or Bollinger configuration")

    @property
    def warmup_requirements(self) -> tuple[tuple[str, int], ...]:
        return (
            ("ema9", self.ema_fast),
            ("ema20", self.ema_trend),
            ("ema50", self.ema_slow),
            ("ema20_slope", self.ema_trend + self.ema_slope_lookback),
            ("rsi14", self.rsi_period + 1),
            ("macd_line", self.macd_slow),
            ("macd_signal", self.macd_slow + self.macd_signal - 1),
            ("atr14", self.atr_period),
            ("adx14", self.adx_period * 2 - 1),
            ("bollinger_bands", self.bollinger_period),
            ("relative_volume", self.relative_volume_lookback + 1),
        )

    @property
    def configuration_hash(self) -> str:
        payload = dict(self.configuration_snapshot)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @property
    def configuration_snapshot(self) -> tuple[tuple[str, str], ...]:
        return (("engine_version", INDICATOR_ENGINE_VERSION),) + tuple(
            (key, str(getattr(self, key))) for key in self.__dataclass_fields__
        )


@dataclass(frozen=True, slots=True)
class TradePrint:
    timestamp: datetime
    instrument_id: str
    price: Decimal
    quantity: Decimal
    source_id: str

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("trade timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        object.__setattr__(self, "price", D(str(self.price)))
        object.__setattr__(self, "quantity", D(str(self.quantity)))
        if (
            not self.price.is_finite()
            or not self.quantity.is_finite()
            or self.price < 0
            or self.quantity <= 0
        ):
            raise ValueError("trade price and quantity must be finite and positive")
        if self.price == 0:
            raise ValueError("trade price and quantity must be finite and positive")
        if not self.instrument_id.strip() or not self.source_id.strip():
            raise ValueError("trade instrument_id and source_id cannot be blank")


@dataclass(frozen=True, slots=True)
class VerifiedVWAP:
    value: Decimal
    source_id: str
    verified: bool
    session_key: str


@dataclass(frozen=True, slots=True)
class VWAPValue:
    value: Decimal | None
    method: str
    exact_trade_level: bool
    session_key: str | None
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndicatorPoint:
    instrument_id: str
    timestamp: datetime
    available_at: datetime
    interval_minutes: int
    close: Decimal | None
    ema9: Decimal | None
    ema20: Decimal | None
    ema50: Decimal | None
    ema20_slope: Decimal | None
    rsi14: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    macd_histogram: Decimal | None
    atr14: Decimal | None
    adx14: Decimal | None
    bollinger_middle: Decimal | None
    bollinger_upper: Decimal | None
    bollinger_lower: Decimal | None
    relative_volume: Decimal | None
    vwap: VWAPValue
    data_valid: bool
    indicator_config_hash: str = ""
    warmup_requirements: tuple[tuple[str, int], ...] = ()
    indicator_configuration: tuple[tuple[str, str], ...] = ()

    def as_snapshot(self) -> tuple[tuple[str, str | None], ...]:
        names = (
            "close",
            "ema9",
            "ema20",
            "ema50",
            "ema20_slope",
            "rsi14",
            "macd",
            "macd_signal",
            "macd_histogram",
            "atr14",
            "adx14",
            "bollinger_middle",
            "bollinger_upper",
            "bollinger_lower",
            "relative_volume",
        )
        values = [
            (name, str(getattr(self, name)) if getattr(self, name) is not None else None)
            for name in names
        ]
        values.extend(
            (
                ("indicator_config_hash", self.indicator_config_hash),
                (
                    "indicator_configuration",
                    ",".join(f"{name}={value}" for name, value in self.indicator_configuration),
                ),
                (
                    "warmup_requirements",
                    ",".join(f"{name}:{period}" for name, period in self.warmup_requirements),
                ),
                ("vwap", str(self.vwap.value) if self.vwap.value is not None else None),
                ("vwap_method", self.vwap.method),
                ("vwap_exact_trade_level", str(self.vwap.exact_trade_level)),
                ("vwap_session_key", self.vwap.session_key),
                ("vwap_source_id", self.vwap.source_id),
            )
        )
        return tuple(values)


def _ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period], D(0)) / period
    out[period - 1] = current
    alpha = D(2) / D(period + 1)
    for index in range(period, len(values)):
        current = alpha * values[index] + (D(1) - alpha) * current
        out[index] = current
    return out


def _wilder(values: Sequence[Decimal | None], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(values)
    valid = [(index, value) for index, value in enumerate(values) if value is not None]
    if len(valid) < period:
        return out
    seed = valid[:period]
    current = sum((value for _, value in seed), D(0)) / period
    out[seed[-1][0]] = current
    for index, value in valid[period:]:
        current = (current * (period - 1) + value) / period
        out[index] = current
    return out


def _rsi(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(closes, closes[1:], strict=False):
        change = current - previous
        gains.append(max(change, D(0)))
        losses.append(max(-change, D(0)))
    avg_gain = sum(gains[:period], D(0)) / period
    avg_loss = sum(losses[:period], D(0)) / period

    def value(gain: Decimal, loss: Decimal) -> Decimal:
        if gain == 0 and loss == 0:
            return D(50)
        if loss == 0:
            return D(100)
        relative_strength = gain / loss
        return D(100) - D(100) / (D(1) + relative_strength)

    out[period] = value(avg_gain, avg_loss)
    for change_index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[change_index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[change_index]) / period
        out[change_index + 1] = value(avg_gain, avg_loss)
    return out


def _atr_adx(
    bars: Sequence[AggregatedCandle], atr_period: int, adx_period: int
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    tr: list[Decimal | None] = []
    plus_dm: list[Decimal | None] = []
    minus_dm: list[Decimal | None] = []
    for index, bar in enumerate(bars):
        if index == 0:
            tr.append(bar.high - bar.low)
            plus_dm.append(D(0))
            minus_dm.append(D(0))
            continue
        previous = bars[index - 1]
        tr.append(
            max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        )
        move_up = bar.high - previous.high
        move_down = previous.low - bar.low
        plus_dm.append(move_up if move_up > move_down and move_up > 0 else D(0))
        minus_dm.append(move_down if move_down > move_up and move_down > 0 else D(0))
    atr = _wilder(tr, atr_period)
    di_true_range = _wilder(tr, adx_period)
    smoothed_plus = _wilder(plus_dm, adx_period)
    smoothed_minus = _wilder(minus_dm, adx_period)
    dx: list[Decimal | None] = [None] * len(bars)
    for index, (plus, minus, true_range) in enumerate(
        zip(smoothed_plus, smoothed_minus, di_true_range, strict=True)
    ):
        if plus is None or minus is None or true_range is None or true_range == 0:
            continue
        plus_di = D(100) * plus / true_range
        minus_di = D(100) * minus / true_range
        denominator = plus_di + minus_di
        dx[index] = D(0) if denominator == 0 else D(100) * abs(plus_di - minus_di) / denominator
    return atr, _wilder(dx, adx_period)


def _bollinger(closes: Sequence[Decimal], period: int, deviations: Decimal):
    middle: list[Decimal | None] = [None] * len(closes)
    upper: list[Decimal | None] = [None] * len(closes)
    lower: list[Decimal | None] = [None] * len(closes)
    for index in range(period - 1, len(closes)):
        window = closes[index - period + 1 : index + 1]
        mean = sum(window, D(0)) / period
        variance = sum(((value - mean) ** 2 for value in window), D(0)) / period
        sigma = variance.sqrt()
        middle[index] = mean
        upper[index] = mean + deviations * sigma
        lower[index] = mean - deviations * sigma
    return middle, upper, lower


def _relative_volume(volumes: Sequence[Decimal], lookback: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(volumes)
    for index in range(lookback, len(volumes)):
        prior = volumes[index - lookback : index]
        mean = sum(prior, D(0)) / lookback
        if mean > 0:
            out[index] = volumes[index] / mean
    return out


def _session_key(
    stamp: datetime,
    instrument: InstrumentMetadata,
    calendar: SessionCalendar,
    interval: int,
    crypto_session_anchor: time,
) -> str | None:
    if instrument.asset_class == "crypto_spot":
        utc_stamp = stamp.astimezone(UTC)
        anchor = datetime.combine(utc_stamp.date(), crypto_session_anchor, UTC)
        if utc_stamp < anchor:
            anchor -= timedelta(days=1)
        return anchor.date().isoformat() + "T" + crypto_session_anchor.isoformat() + "Z"
    session = calendar.session_for_bar(stamp, timedelta(minutes=interval))
    return session.session_date.isoformat() if session else None


def _vwap_values(
    bars: Sequence[AggregatedCandle],
    instrument: InstrumentMetadata,
    calendar: SessionCalendar,
    *,
    trades: Sequence[TradePrint] | None,
    verified_values: dict[datetime, VerifiedVWAP] | None,
    crypto_session_anchor: time,
) -> list[VWAPValue]:
    if trades is not None and verified_values is not None:
        raise ValueError("provide actual trades or verified source VWAP, not both")
    if trades is not None:
        if any(trade.instrument_id != instrument.instrument_id for trade in trades):
            raise ValueError("trade print instrument does not match selected instrument")
        if [trade.timestamp for trade in trades] != sorted(trade.timestamp for trade in trades):
            raise ValueError("trade prints must be chronological")
    for stamp, verified in (verified_values or {}).items():
        if (
            stamp.tzinfo is None
            or stamp.utcoffset() is None
            or not verified.verified
            or not verified.source_id.strip()
        ):
            raise ValueError(
                "verified VWAP values require aware timestamps and verified source provenance"
            )
        if not D(str(verified.value)).is_finite() or D(str(verified.value)) < 0:
            raise ValueError("verified VWAP cannot be negative")
    output: list[VWAPValue] = []
    cumulative_notional: dict[str, Decimal] = {}
    cumulative_volume: dict[str, Decimal] = {}
    invalid_sessions: set[str] = set()
    previous_by_session: dict[str, AggregatedCandle] = {}
    trade_sources: dict[str, set[str]] = {}
    trade_index = 0
    for bar in bars:
        key = _session_key(
            bar.timestamp, instrument, calendar, bar.interval_minutes, crypto_session_anchor
        )
        if not bar.is_valid or key is None:
            if key is not None and trades is None and verified_values is None:
                invalid_sessions.add(key)
            output.append(VWAPValue(None, "unavailable", False, key))
            continue
        if verified_values is not None:
            provided = verified_values.get(bar.timestamp)
            if provided is None:
                output.append(VWAPValue(None, "verified_source_missing", False, key))
            else:
                if provided.session_key != key:
                    raise ValueError(
                        "verified VWAP session key does not match the instrument session"
                    )
                output.append(
                    VWAPValue(
                        D(str(provided.value)),
                        "verified_source_vwap",
                        True,
                        key,
                        provided.source_id,
                    )
                )
            continue
        if trades is not None:
            cutoff = bar.end_timestamp
            while trade_index < len(trades) and trades[trade_index].timestamp < cutoff:
                trade = trades[trade_index]
                trade_key = _session_key(
                    trade.timestamp,
                    instrument,
                    calendar,
                    bar.interval_minutes,
                    crypto_session_anchor,
                )
                if trade_key == key:
                    cumulative_notional[key] = (
                        cumulative_notional.get(key, D(0)) + trade.price * trade.quantity
                    )
                    cumulative_volume[key] = cumulative_volume.get(key, D(0)) + trade.quantity
                    trade_sources.setdefault(key, set()).add(trade.source_id)
                trade_index += 1
            volume = cumulative_volume.get(key, D(0))
            notional = cumulative_notional.get(key, D(0))
            output.append(
                VWAPValue(
                    notional / volume if volume else None,
                    "trade_level",
                    True,
                    key,
                    ",".join(sorted(trade_sources.get(key, set()))) or None,
                )
            )
            continue
        previous = previous_by_session.get(key)
        if previous is None:
            session = (
                calendar.session_for_bar(bar.timestamp, timedelta(minutes=bar.interval_minutes))
                if instrument.asset_class != "crypto_spot"
                else None
            )
            session_open = (
                session.open_utc
                if session
                else datetime.combine(bar.timestamp.date(), crypto_session_anchor, UTC)
            )
            if instrument.asset_class == "crypto_spot" and bar.timestamp < session_open:
                session_open -= timedelta(days=1)
            if bar.timestamp != session_open:
                invalid_sessions.add(key)
        elif bar.timestamp != previous.timestamp + timedelta(minutes=bar.interval_minutes):
            invalid_sessions.add(key)
        previous_by_session[key] = bar
        if key in invalid_sessions:
            output.append(VWAPValue(None, "unavailable_incomplete_session_data", False, key))
            continue
        typical = (bar.high + bar.low + bar.close) / D(3)
        cumulative_notional[key] = cumulative_notional.get(key, D(0)) + typical * bar.volume
        cumulative_volume[key] = cumulative_volume.get(key, D(0)) + bar.volume
        volume = cumulative_volume[key]
        output.append(
            VWAPValue(
                cumulative_notional[key] / volume if volume else None,
                "ohlcv_typical_price_approximation" if volume else "unavailable_zero_volume",
                False,
                key,
            )
        )
    return output


def compute_indicators(
    bars: Sequence[AggregatedCandle],
    instrument: InstrumentMetadata,
    *,
    config: IndicatorConfig | None = None,
    trades: Sequence[TradePrint] | None = None,
    verified_vwap: dict[datetime, VerifiedVWAP] | None = None,
    crypto_session_anchor_utc: time = time(0, 0),
) -> tuple[IndicatorPoint, ...]:
    """Compute causal indicators; every point is available at its candle end."""
    if not bars:
        return ()
    config = config or IndicatorConfig()
    interval = bars[0].interval_minutes
    if any(
        bar.interval_minutes != interval or bar.instrument_id != instrument.instrument_id
        for bar in bars
    ):
        raise ValueError("indicator input must use one interval and selected instrument")
    timestamps = [bar.timestamp for bar in bars]
    if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
        raise ValueError("indicator input must be unique and chronological")
    if any(bar.end_timestamp - bar.timestamp != timedelta(minutes=interval) for bar in bars):
        raise ValueError("indicator input contains candles that are not complete interval bars")
    calendar = SessionCalendar(instrument.session_calendar)
    vwap = _vwap_values(
        bars,
        instrument,
        calendar,
        trades=trades,
        verified_values=verified_vwap,
        crypto_session_anchor=crypto_session_anchor_utc,
    )
    result: list[IndicatorPoint | None] = [None] * len(bars)
    block_start = 0

    def emit_block(start: int, end: int) -> None:
        block = bars[start:end]
        closes = [bar.close for bar in block]
        volumes = [bar.volume for bar in block]
        ema9 = _ema(closes, config.ema_fast)
        ema20 = _ema(closes, config.ema_trend)
        ema50 = _ema(closes, config.ema_slow)
        ema12 = _ema(closes, config.macd_fast)
        ema26 = _ema(closes, config.macd_slow)
        macd = [
            a - b if a is not None and b is not None else None
            for a, b in zip(ema12, ema26, strict=True)
        ]
        macd_signal = _ema_sparse(macd, config.macd_signal)
        histogram = [
            a - b if a is not None and b is not None else None
            for a, b in zip(macd, macd_signal, strict=True)
        ]
        rsi = _rsi(closes, config.rsi_period)
        atr, adx = _atr_adx(block, config.atr_period, config.adx_period)
        middle, upper, lower = _bollinger(
            closes, config.bollinger_period, config.bollinger_deviations
        )
        relative_volume = _relative_volume(volumes, config.relative_volume_lookback)
        for local_index, bar in enumerate(block):
            slope_index = local_index - config.ema_slope_lookback
            slope = (
                ema20[local_index] - ema20[slope_index]
                if slope_index >= 0
                and ema20[local_index] is not None
                and ema20[slope_index] is not None
                else None
            )
            index = start + local_index
            result[index] = IndicatorPoint(
                instrument.instrument_id,
                bar.timestamp,
                bar.end_timestamp,
                interval,
                bar.close,
                ema9[local_index],
                ema20[local_index],
                ema50[local_index],
                slope,
                rsi[local_index],
                macd[local_index],
                macd_signal[local_index],
                histogram[local_index],
                atr[local_index],
                adx[local_index],
                middle[local_index],
                upper[local_index],
                lower[local_index],
                relative_volume[local_index],
                vwap[index],
                True,
                config.configuration_hash,
                config.warmup_requirements,
                config.configuration_snapshot,
            )

    for index, bar in enumerate(bars):
        broken = not bar.is_valid
        if index > block_start and not broken:
            previous = bars[index - 1]
            current_session = _session_key(
                bar.timestamp, instrument, calendar, interval, crypto_session_anchor_utc
            )
            previous_session = _session_key(
                previous.timestamp, instrument, calendar, interval, crypto_session_anchor_utc
            )
            if (
                current_session == previous_session
                and bar.timestamp != previous.timestamp + timedelta(minutes=interval)
            ):
                emit_block(block_start, index)
                block_start = index
        if broken:
            if index > block_start:
                emit_block(block_start, index)
            result[index] = IndicatorPoint(
                instrument.instrument_id,
                bar.timestamp,
                bar.end_timestamp,
                interval,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                vwap[index],
                False,
                config.configuration_hash,
                config.warmup_requirements,
                config.configuration_snapshot,
            )
            block_start = index + 1
    if block_start < len(bars):
        emit_block(block_start, len(bars))
    return tuple(point for point in result if point is not None)


def _ema_sparse(values: Sequence[Decimal | None], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(values)
    indices = [index for index, value in enumerate(values) if value is not None]
    if len(indices) < period:
        return out
    seed = indices[:period]
    current = sum((values[index] for index in seed), D(0)) / period
    out[seed[-1]] = current
    alpha = D(2) / D(period + 1)
    for index in indices[period:]:
        current = alpha * values[index] + (D(1) - alpha) * current
        out[index] = current
    return out
