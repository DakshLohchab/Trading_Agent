"""Long-only strategy research rules. This module never creates or submits orders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.candidates import (
    CandidateRegistry,
    QuoteSnapshot,
    SetupStatus,
    StrategyEvaluation,
    hash_configuration,
    make_candidate,
)
from tradingagents.intraday.costs import UnavailableCostEstimator
from tradingagents.intraday.indicators import IndicatorPoint
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata
from tradingagents.intraday.regimes import MarketRegime, RegimeConfig, classify_regime

D = Decimal
VWAP_PULLBACK_ID = "vwap_trend_pullback"
CRYPTO_VWAP_PULLBACK_ID = "crypto_vwap_trend_pullback"
ORB_ID = "opening_range_breakout"
RANGE_MEAN_REVERSION_ENABLED = False


@dataclass(frozen=True, slots=True)
class VWAPPullbackConfig:
    version: str = "1.0.0"
    primary_interval_minutes: int = 5
    confirmation_interval_minutes: int = 15
    pullback_lookback_bars: int = 3
    pullback_zone_atr: Decimal = D("0.35")
    max_vwap_penetration_atr: Decimal = D("0.50")
    max_extension_atr: Decimal = D("1.50")
    min_atr_percent: Decimal = D("0.0005")
    max_atr_percent: Decimal = D("0.04")
    min_relative_volume: Decimal = D("0.75")
    stop_atr_multiple: Decimal = D("1.25")
    target_reward_to_risk: Decimal = D("2.0")
    expiry_bars: int = 3
    max_spread_bps: Decimal = D("15")
    max_quote_age_minutes: int = 5
    require_quotes: bool = False
    crypto_session_anchor_utc: time | None = None
    regime: RegimeConfig = RegimeConfig()

    def __post_init__(self) -> None:
        if self.primary_interval_minutes != 5 or self.confirmation_interval_minutes != 15:
            raise ValueError("VWAP Trend Pullback uses 5-minute and 15-minute bars")
        if (
            self.pullback_lookback_bars <= 0
            or self.expiry_bars <= 0
            or self.max_quote_age_minutes <= 0
        ):
            raise ValueError("pullback lookback and expiry bars must be positive")
        for field in (
            "pullback_zone_atr",
            "max_vwap_penetration_atr",
            "max_extension_atr",
            "min_atr_percent",
            "max_atr_percent",
            "min_relative_volume",
            "stop_atr_multiple",
            "target_reward_to_risk",
            "max_spread_bps",
        ):
            value = D(str(getattr(self, field)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field} must be a finite positive decimal")
            object.__setattr__(self, field, value)
        if self.min_atr_percent >= self.max_atr_percent:
            raise ValueError("min_atr_percent must be below max_atr_percent")
        if (
            self.crypto_session_anchor_utc is not None
            and self.crypto_session_anchor_utc.tzinfo is not None
        ):
            raise ValueError("crypto session anchor must be a naive UTC wall-clock time")


@dataclass(frozen=True, slots=True)
class CryptoVWAPPullbackConfig:
    """Versioned BTC spot VWAP semantics, separate from the equity strategy."""

    version: str = "crypto-vwap-ohlcv-1.0.0"
    utc_anchor: time = time(0, 0)
    price_input: str = "ohlcv_typical_price=(high+low+close)/3"
    volume_input: str = "binance_kline_base_volume"
    candle_completion: str = "bar_end_timestamp"
    pullback: VWAPPullbackConfig = VWAPPullbackConfig()

    def __post_init__(self) -> None:
        if not self.version.strip() or self.utc_anchor != time(0, 0):
            raise ValueError("crypto VWAP currently requires a version and 00:00 UTC reset")
        if self.price_input != "ohlcv_typical_price=(high+low+close)/3":
            raise ValueError("crypto VWAP price input must remain the documented OHLCV approximation")
        if self.volume_input != "binance_kline_base_volume":
            raise ValueError("crypto VWAP volume input must be Binance kline base volume")
        if self.candle_completion != "bar_end_timestamp":
            raise ValueError("crypto VWAP decisions require completed candles")

    def strategy_config(self) -> VWAPPullbackConfig:
        from dataclasses import replace

        return replace(
            self.pullback,
            version=self.version,
            crypto_session_anchor_utc=self.utc_anchor,
        )


@dataclass(frozen=True, slots=True)
class OpeningRangeConfig:
    version: str = "1.0.0"
    opening_range_minutes: int = 15
    candle_interval_minutes: int = 5
    min_relative_volume: Decimal = D("1.50")
    stop_atr_multiple: Decimal = D("1.50")
    target_reward_to_risk: Decimal = D("2.0")
    expiry_minutes: int = 30
    max_atr_percent: Decimal = D("0.04")
    max_spread_bps: Decimal = D("15")
    max_quote_age_minutes: int = 5
    require_quotes: bool = False
    require_retest: bool = False
    retest_window_bars: int = 3
    allow_crypto: bool = False
    crypto_session_anchor_utc: time | None = None
    regime: RegimeConfig = RegimeConfig()
    allowed_regimes: tuple[MarketRegime, ...] = (MarketRegime.TRENDING_UP, MarketRegime.RANGE_BOUND)

    def __post_init__(self) -> None:
        if self.opening_range_minutes <= 0 or self.candle_interval_minutes != 5:
            raise ValueError("ORB requires a positive opening range and 5-minute candles")
        if self.opening_range_minutes % self.candle_interval_minutes:
            raise ValueError("opening range must be divisible by the candle interval")
        if (
            self.expiry_minutes <= 0
            or self.retest_window_bars <= 0
            or self.max_quote_age_minutes <= 0
        ):
            raise ValueError("expiry and retest window must be positive")
        for field in (
            "min_relative_volume",
            "stop_atr_multiple",
            "target_reward_to_risk",
            "max_atr_percent",
            "max_spread_bps",
        ):
            value = D(str(getattr(self, field)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field} must be a finite positive decimal")
            object.__setattr__(self, field, value)
        if self.allow_crypto and self.crypto_session_anchor_utc is None:
            raise ValueError("experimental crypto ORB requires an explicit UTC session anchor")
        if (
            self.crypto_session_anchor_utc is not None
            and self.crypto_session_anchor_utc.tzinfo is not None
        ):
            raise ValueError("crypto session anchor must be a naive UTC wall-clock time")
        forbidden = {
            MarketRegime.UNKNOWN,
            MarketRegime.DISLOCATED,
            MarketRegime.HIGH_VOLATILITY,
        }
        if forbidden.intersection(self.allowed_regimes):
            raise ValueError("ORB cannot allow UNKNOWN, DISLOCATED or HIGH_VOLATILITY regimes")


def _validate_inputs(
    instrument: InstrumentMetadata,
    bars: Sequence[AggregatedCandle],
    points: Sequence[IndicatorPoint],
    interval: int,
) -> tuple[dict[datetime, AggregatedCandle], dict[datetime, IndicatorPoint]]:
    if [bar.timestamp for bar in bars] != sorted(bar.timestamp for bar in bars):
        raise ValueError("strategy bars must be chronological")
    if len({bar.timestamp for bar in bars}) != len(bars):
        raise ValueError("strategy bars must not contain duplicate timestamps")
    bar_map = {}
    for bar in bars:
        if bar.instrument_id != instrument.instrument_id or bar.interval_minutes != interval:
            raise ValueError("strategy bars do not match instrument/timeframe")
        bar_map[bar.timestamp] = bar
    point_map = {}
    config_signatures = set()
    for point in points:
        if point.instrument_id != instrument.instrument_id or point.interval_minutes != interval:
            raise ValueError("indicator points do not match instrument/timeframe")
        if point.timestamp in point_map or point.timestamp not in bar_map:
            raise ValueError("indicator points must uniquely match input bars")
        if point.available_at != bar_map[point.timestamp].end_timestamp:
            raise ValueError("indicator availability must equal the completed candle end")
        if (
            not point.indicator_config_hash
            or not point.indicator_configuration
            or not point.warmup_requirements
        ):
            raise ValueError(
                "indicator points must record their configuration and warm-up requirements"
            )
        config_signatures.add(
            (point.indicator_config_hash, point.indicator_configuration, point.warmup_requirements)
        )
        point_map[point.timestamp] = point
    if len(config_signatures) > 1:
        raise ValueError("indicator configuration cannot change within a strategy input series")
    return bar_map, point_map


def _session_bounds(
    instrument: InstrumentMetadata,
    timestamp: datetime,
    *,
    crypto_anchor: time | None = None,
) -> tuple[str, datetime, datetime] | None:
    if instrument.asset_class == "crypto_spot":
        if crypto_anchor is None:
            return None
        utc = timestamp.astimezone(UTC)
        start = datetime.combine(utc.date(), crypto_anchor, UTC)
        if utc < start:
            start -= timedelta(days=1)
        return (start.isoformat(), start, start + timedelta(days=1))
    calendar = SessionCalendar(instrument.session_calendar)
    session = calendar.session_for_bar(timestamp)
    if session is None:
        return None
    return session.session_date.isoformat(), session.open_utc, session.close_utc


def _expected_vwap_session_key(
    instrument: InstrumentMetadata, session: tuple[str, datetime, datetime], anchor: time | None
) -> str:
    if instrument.asset_class == "crypto_spot":
        return session[1].date().isoformat() + "T" + anchor.isoformat() + "Z"
    return session[0]


def _quote_quality(
    quote,
    decision_at: datetime,
    config_max_bps: Decimal,
    require_quotes: bool,
    max_age: timedelta,
) -> tuple[bool, str, Decimal | None]:
    if quote is None:
        return (
            not require_quotes,
            "quotes_unavailable" if not require_quotes else "quotes_required",
            None,
        )
    if quote.available_at > decision_at:
        return False, "quote_not_yet_available", None
    if decision_at - quote.timestamp > max_age:
        return False, "quote_stale", None
    midpoint = (quote.ask + quote.bid) / 2
    spread_bps = (quote.ask - quote.bid) / midpoint * D(10000)
    if spread_bps > config_max_bps:
        return False, "spread_limit_exceeded", spread_bps
    return True, "quote_valid", spread_bps


def _snapshot(
    *named_points: tuple[str, IndicatorPoint], extra: tuple[tuple[str, str | None], ...] = ()
):
    values: list[tuple[str, str | None]] = []
    for prefix, point in named_points:
        values.extend((prefix + name, value) for name, value in point.as_snapshot())
    values.extend(extra)
    return tuple(values)


def _config_snapshot(config) -> tuple[tuple[str, str | None], ...]:
    values: list[tuple[str, str | None]] = []

    def visit(name: str, value) -> None:
        if is_dataclass(value):
            for field in fields(value):
                visit(f"{name}.{field.name}", getattr(value, field.name))
        elif isinstance(value, tuple):
            values.append(
                (
                    name,
                    ",".join(
                        item.value if isinstance(item, MarketRegime) else str(item)
                        for item in value
                    ),
                )
            )
        elif value is None:
            values.append((name, None))
        elif isinstance(value, Decimal):
            values.append((name, str(value)))
        elif isinstance(value, time):
            values.append((name, value.isoformat()))
        elif isinstance(value, MarketRegime):
            values.append((name, value.value))
        else:
            values.append((name, str(value)))

    visit("strategy_config", config)
    return tuple(values)


def _candle_snapshot(prefix: str, bar: AggregatedCandle) -> tuple[tuple[str, str | None], ...]:
    values = (
        ("timestamp", bar.timestamp.isoformat()),
        ("end_timestamp", bar.end_timestamp.isoformat()),
        ("open", str(bar.open) if bar.open is not None else None),
        ("high", str(bar.high) if bar.high is not None else None),
        ("low", str(bar.low) if bar.low is not None else None),
        ("close", str(bar.close) if bar.close is not None else None),
        ("volume", str(bar.volume) if bar.volume is not None else None),
        ("is_valid", str(bar.is_valid)),
        ("source_ids", ",".join(bar.source_ids)),
    )
    return tuple((prefix + name, value) for name, value in values)


def _instrument_snapshot(instrument: InstrumentMetadata) -> tuple[tuple[str, str | None], ...]:
    names = (
        "base_currency",
        "quote_currency",
        "price_precision",
        "tick_size",
        "quantity_increment",
        "min_order_quantity",
        "min_order_notional",
        "exchange_timezone",
        "session_calendar",
        "provenance",
    )
    return tuple(
        (
            "instrument." + name,
            str(getattr(instrument, name)) if getattr(instrument, name) is not None else None,
        )
        for name in names
    )


def _quote_snapshot(quote: QuoteSnapshot | None) -> tuple[tuple[str, str | None], ...]:
    if quote is None:
        return (
            ("quote.timestamp", None),
            ("quote.available_at", None),
            ("quote.bid", None),
            ("quote.ask", None),
            ("quote.source_id", None),
        )
    return (
        ("quote.timestamp", quote.timestamp.isoformat()),
        ("quote.available_at", quote.available_at.isoformat()),
        ("quote.bid", str(quote.bid)),
        ("quote.ask", str(quote.ask)),
        ("quote.source_id", quote.source_id),
    )


def evaluate_vwap_trend_pullback(
    instrument: InstrumentMetadata,
    bars_5m: Sequence[AggregatedCandle],
    indicators_5m: Sequence[IndicatorPoint],
    bars_15m: Sequence[AggregatedCandle],
    indicators_15m: Sequence[IndicatorPoint],
    *,
    replay_id: str,
    config: VWAPPullbackConfig | None = None,
    registry: CandidateRegistry | None = None,
    quotes: dict[datetime, QuoteSnapshot] | None = None,
    strategy_id_override: str | None = None,
    strategy_metadata: tuple[tuple[str, str], ...] = (),
) -> tuple[StrategyEvaluation, ...]:
    """Evaluate completed 5m reclaim bars with as-of-only 15m confirmation."""
    if not replay_id.strip():
        raise ValueError("replay_id is required for auditable candidates")
    config = config or VWAPPullbackConfig()
    if instrument.asset_class == "crypto_spot" and config.crypto_session_anchor_utc is None:
        raise ValueError("crypto VWAP pullback requires an explicit UTC session anchor")
    bars5, points5 = _validate_inputs(instrument, bars_5m, indicators_5m, 5)
    bars15, points15 = _validate_inputs(instrument, bars_15m, indicators_15m, 15)
    registry = registry or CandidateRegistry()
    output: list[StrategyEvaluation] = []
    emitted_points = sorted(points15.values(), key=lambda point: point.available_at)
    config_inputs = (
        config,
        tuple(sorted({point.indicator_config_hash for point in indicators_5m})),
        tuple(sorted({point.indicator_config_hash for point in indicators_15m})),
    )
    config_hash = hash_configuration(
        config_inputs + ((strategy_id_override, strategy_metadata),)
        if strategy_id_override
        else config_inputs
    )
    cost_estimator = UnavailableCostEstimator()
    for index, bar in enumerate(bars_5m):
        decision_at = bar.end_timestamp
        point = points5.get(bar.timestamp)
        if not bar.is_valid or point is None or not point.data_valid:
            output.append(
                StrategyEvaluation(SetupStatus.INVALID_DATA, "invalid_or_unavailable_5m_data")
            )
            continue
        confirmation = next(
            (
                candidate
                for candidate in reversed(emitted_points)
                if candidate.available_at <= decision_at
            ),
            None,
        )
        if confirmation is None:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "higher_timeframe_not_yet_complete")
            )
            continue
        confirmation_bar = bars15[confirmation.timestamp]
        if not confirmation_bar.is_valid or not confirmation.data_valid:
            output.append(
                StrategyEvaluation(SetupStatus.INVALID_DATA, "invalid_15m_confirmation_data")
            )
            continue
        if (
            confirmation.close is None
            or confirmation.ema20 is None
            or confirmation.ema50 is None
            or confirmation.ema20_slope is None
            or confirmation.close <= confirmation.ema20
            or confirmation.ema20 <= confirmation.ema50
            or confirmation.ema20_slope <= 0
        ):
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "15m_trend_confirmation_failed"))
            continue
        if point.vwap.value is None or point.atr14 is None or point.relative_volume is None:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "vwap_or_liquidity_warmup"))
            continue
        session = _session_bounds(
            instrument, bar.timestamp, crypto_anchor=config.crypto_session_anchor_utc
        )
        if session is None or decision_at >= session[2]:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "outside_or_at_session_close"))
            continue
        vwap_session_key = _expected_vwap_session_key(
            instrument, session, config.crypto_session_anchor_utc
        )
        if point.vwap.session_key != vwap_session_key:
            output.append(
                StrategyEvaluation(SetupStatus.INVALID_DATA, "vwap_session_anchor_mismatch")
            )
            continue
        previous_candidates = []
        for prior in bars_5m[max(0, index - config.pullback_lookback_bars) : index]:
            prior_point = points5.get(prior.timestamp)
            prior_session = _session_bounds(
                instrument, prior.timestamp, crypto_anchor=config.crypto_session_anchor_utc
            )
            if (
                prior.is_valid
                and prior_point
                and prior_point.data_valid
                and prior_point.vwap.value is not None
                and prior_point.vwap.session_key == vwap_session_key
                and prior_point.atr14 is not None
                and session
                and prior_session
                and prior_session[0] == session[0]
                and prior.low
                <= prior_point.vwap.value + prior_point.atr14 * config.pullback_zone_atr
                and prior.low
                >= prior_point.vwap.value - prior_point.atr14 * config.max_vwap_penetration_atr
            ):
                previous_candidates.append((prior, prior_point))
        if not previous_candidates:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "no_recent_vwap_pullback"))
            continue
        prior_bar = bars_5m[index - 1] if index else None
        if prior_bar is None or bar.close <= bar.open or bar.close <= prior_bar.high:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "bullish_reclaim_or_structure_failed")
            )
            continue
        if bar.close <= point.vwap.value:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "close_not_above_session_vwap"))
            continue
        extension = (bar.close - point.vwap.value) / point.atr14
        atr_percent = point.atr14 / abs(bar.close) if bar.close else D(0)
        if extension > config.max_extension_atr:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "price_overextended_from_vwap"))
            continue
        if not config.min_atr_percent <= atr_percent <= config.max_atr_percent:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "volatility_condition_failed"))
            continue
        if point.relative_volume < config.min_relative_volume:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "relative_volume_condition_failed")
            )
            continue
        previous_close = prior_bar.close if prior_bar else None
        regime = classify_regime(point, previous_close=previous_close, config=config.regime)
        if regime.regime != MarketRegime.TRENDING_UP:
            output.append(
                StrategyEvaluation(
                    SetupStatus.REJECTED,
                    "regime_does_not_permit_vwap_pullback:" + regime.regime.value,
                )
            )
            continue
        quote = (quotes or {}).get(bar.timestamp)
        quote_ok, quote_status, spread_bps = _quote_quality(
            quote,
            decision_at,
            config.max_spread_bps,
            config.require_quotes,
            timedelta(minutes=config.max_quote_age_minutes),
        )
        if not quote_ok:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, quote_status))
            continue
        trigger = bar.high + instrument.tick_size
        structure_low = min(candidate[0].low for candidate in previous_candidates)
        stop = min(
            structure_low - instrument.tick_size, trigger - point.atr14 * config.stop_atr_multiple
        )
        invalidation = structure_low
        target = trigger + (trigger - stop) * config.target_reward_to_risk
        expiry = min(decision_at + timedelta(minutes=5 * config.expiry_bars), session[2])
        if expiry <= decision_at:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "insufficient_session_time_for_candidate")
            )
            continue
        extra = (
            (
                ("regime", regime.regime.value),
                ("confirmation_available_at", confirmation.available_at.isoformat()),
                ("confirmation_timestamp", confirmation.timestamp.isoformat()),
                (
                    "pullback_candle_timestamps",
                    ",".join(item[0].timestamp.isoformat() for item in previous_candidates),
                ),
                ("quote_spread_bps", str(spread_bps) if spread_bps is not None else None),
                ("quote_status", quote_status),
            )
            + _candle_snapshot("reference_5m.", bar)
            + _candle_snapshot("confirmation_15m.", confirmation_bar)
            + tuple(
                item
                for session_bar in bars_5m[: index + 1]
                if (
                    (
                        bar_session := _session_bounds(
                            instrument,
                            session_bar.timestamp,
                            crypto_anchor=config.crypto_session_anchor_utc,
                        )
                    )
                    is not None
                    and bar_session[0] == session[0]
                )
                for item in _candle_snapshot(
                    "vwap_session_5m." + session_bar.timestamp.isoformat() + ".", session_bar
                )
            )
            + tuple(
                item
                for pullback_bar, _ in previous_candidates
                for item in _candle_snapshot(
                    "pullback_5m." + pullback_bar.timestamp.isoformat() + ".", pullback_bar
                )
            )
            + _instrument_snapshot(instrument)
            + _quote_snapshot(quote)
            + _config_snapshot(config)
            + tuple(("crypto_strategy." + key, value) for key, value in strategy_metadata)
        )
        point_inputs = [("5m.reclaim.", point), ("15m.confirmation.", confirmation)]
        point_inputs.extend(
            ("5m.pullback." + pullback_bar.timestamp.isoformat() + ".", pullback_point)
            for pullback_bar, pullback_point in previous_candidates
        )
        candidate = make_candidate(
            instrument=instrument,
            strategy_id=(strategy_id_override or (
                CRYPTO_VWAP_PULLBACK_ID
                if instrument.asset_class == "crypto_spot"
                else VWAP_PULLBACK_ID
            )),
            strategy_version=config.version,
            decision_timestamp=decision_at,
            reference_candle_timestamp=bar.timestamp,
            entry_trigger_type="stop_above_reclaim_candle_high",
            entry_trigger_price=trigger,
            stop_price=stop,
            invalidation_price=invalidation,
            target_price=target,
            expiry_timestamp=expiry,
            market_regime=regime.regime,
            indicator_snapshot=_snapshot(*point_inputs, extra=extra),
            data_quality_status="valid_quotes_" + quote_status,
            replay_id=replay_id,
            configuration_hash=config_hash,
            cost_estimate=cost_estimator.estimate(
                entry=trigger, exit_price=target, instrument_id=instrument.instrument_id
            ),
        )
        output.append(registry.emit(candidate, now=decision_at))
    return tuple(output)


def evaluate_crypto_vwap_trend_pullback(
    instrument: InstrumentMetadata,
    bars_5m: Sequence[AggregatedCandle],
    indicators_5m: Sequence[IndicatorPoint],
    bars_15m: Sequence[AggregatedCandle],
    indicators_15m: Sequence[IndicatorPoint],
    *,
    replay_id: str,
    config: CryptoVWAPPullbackConfig | None = None,
    registry: CandidateRegistry | None = None,
) -> tuple[StrategyEvaluation, ...]:
    """Evaluate the distinct BTC spot strategy with fixed UTC-day VWAP semantics."""
    if instrument.asset_class != "crypto_spot" or instrument.session_calendar != "24/7":
        raise ValueError("crypto VWAP requires an explicitly configured 24/7 spot instrument")
    selected = config or CryptoVWAPPullbackConfig()
    all_points = (*indicators_5m, *indicators_15m)
    if any(
        point.vwap.method != "ohlcv_typical_price_approximation"
        or point.vwap.exact_trade_level
        for point in all_points
    ):
        raise ValueError("crypto VWAP requires the OHLCV typical-price approximation")
    return evaluate_vwap_trend_pullback(
        instrument,
        bars_5m,
        indicators_5m,
        bars_15m,
        indicators_15m,
        replay_id=replay_id,
        config=selected.strategy_config(),
        registry=registry,
        strategy_id_override=CRYPTO_VWAP_PULLBACK_ID,
        strategy_metadata=(
            ("version", selected.version),
            ("utc_anchor", selected.utc_anchor.isoformat()),
            ("price_input", selected.price_input),
            ("volume_input", selected.volume_input),
            ("candle_completion", selected.candle_completion),
        ),
    )


def evaluate_opening_range_breakout(
    instrument: InstrumentMetadata,
    bars_5m: Sequence[AggregatedCandle],
    indicators_5m: Sequence[IndicatorPoint],
    *,
    replay_id: str,
    config: OpeningRangeConfig | None = None,
    registry: CandidateRegistry | None = None,
    quotes: dict[datetime, QuoteSnapshot] | None = None,
) -> tuple[StrategyEvaluation, ...]:
    """Evaluate a completed opening range and close-confirmed 5m breakout."""
    if not replay_id.strip():
        raise ValueError("replay_id is required for auditable candidates")
    config = config or OpeningRangeConfig()
    if instrument.asset_class == "crypto_spot" and (
        not config.allow_crypto or config.crypto_session_anchor_utc is None
    ):
        return (StrategyEvaluation(SetupStatus.REJECTED, "crypto_orb_disabled_by_default"),)
    if instrument.asset_class == "equity" and config.allow_crypto:
        raise ValueError("allow_crypto only configures an explicit crypto experiment")
    bars, points = _validate_inputs(instrument, bars_5m, indicators_5m, 5)
    registry = registry or CandidateRegistry()
    config_hash = hash_configuration(
        (config, tuple(sorted({point.indicator_config_hash for point in indicators_5m})))
    )
    cost_estimator = UnavailableCostEstimator()
    output: list[StrategyEvaluation] = []
    by_session: dict[str, list[AggregatedCandle]] = {}
    for bar in bars_5m:
        if instrument.asset_class == "crypto_spot":
            session = _session_bounds(
                instrument, bar.timestamp, crypto_anchor=config.crypto_session_anchor_utc
            )
        else:
            session = _session_bounds(instrument, bar.timestamp)
        if session:
            by_session.setdefault(session[0], []).append(bar)
    emitted_result_for_session: dict[str, StrategyEvaluation] = {}
    for bar in bars_5m:
        point = points.get(bar.timestamp)
        session = (
            _session_bounds(
                instrument, bar.timestamp, crypto_anchor=config.crypto_session_anchor_utc
            )
            if instrument.asset_class == "crypto_spot"
            else _session_bounds(instrument, bar.timestamp)
        )
        if instrument.asset_class == "crypto_spot" and session is None:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "crypto_orb_session_unavailable")
            )
            continue
        if session is None:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "outside_exchange_session"))
            continue
        session_key, session_open, session_close = session
        if session_key in emitted_result_for_session:
            prior_result = emitted_result_for_session[session_key]
            output.append(
                StrategyEvaluation(
                    SetupStatus.DUPLICATE,
                    "session_candidate_already_emitted",
                    prior_result.candidate,
                )
            )
            continue
        decision_at = bar.end_timestamp
        range_end = session_open + timedelta(minutes=config.opening_range_minutes)
        if decision_at <= range_end:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "opening_range_not_yet_complete")
            )
            continue
        if not bar.is_valid or point is None or not point.data_valid:
            result = StrategyEvaluation(
                SetupStatus.INVALID_DATA, "invalid_or_unavailable_breakout_bar"
            )
            output.append(result)
            continue
        session_bars = by_session[session_key]
        range_count = config.opening_range_minutes // config.candle_interval_minutes
        wanted = [
            session_open + timedelta(minutes=config.candle_interval_minutes * index)
            for index in range(range_count)
        ]
        session_map = {candidate.timestamp: candidate for candidate in session_bars}
        range_bars = [session_map.get(stamp) for stamp in wanted]
        if any(candidate is None or not candidate.is_valid for candidate in range_bars):
            output.append(
                StrategyEvaluation(
                    SetupStatus.INVALID_DATA, "opening_range_incomplete_or_missing_bars"
                )
            )
            continue
        valid_range_bars = [candidate for candidate in range_bars if candidate is not None]
        range_high = max(candidate.high for candidate in valid_range_bars)
        range_low = min(candidate.low for candidate in valid_range_bars)
        coverage_count = (
            int(
                (bar.timestamp - session_open).total_seconds()
                // 60
                // config.candle_interval_minutes
            )
            + 1
        )
        expected_since_open = [
            session_open + timedelta(minutes=config.candle_interval_minutes * offset)
            for offset in range(coverage_count)
        ]
        if any(
            stamp not in session_map or not session_map[stamp].is_valid
            for stamp in expected_since_open
        ):
            output.append(
                StrategyEvaluation(SetupStatus.INVALID_DATA, "missing_or_invalid_session_candle")
            )
            continue
        if bar.timestamp < range_end:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "bar_belongs_to_opening_range"))
            continue
        if bar.close <= range_high:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "no_close_above_opening_range"))
            continue
        if point.atr14 is None or point.relative_volume is None:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "breakout_indicator_warmup"))
            continue
        if point.relative_volume < config.min_relative_volume:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "relative_volume_condition_failed")
            )
            continue
        atr_percent = point.atr14 / abs(bar.close) if bar.close else D(0)
        if atr_percent > config.max_atr_percent:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, "volatility_condition_failed"))
            continue
        prior_close = None
        prior_bars = [
            candidate for candidate in session_bars if candidate.timestamp < bar.timestamp
        ]
        if prior_bars:
            prior_close = prior_bars[-1].close
        regime = classify_regime(point, previous_close=prior_close, config=config.regime)
        if regime.regime not in config.allowed_regimes:
            output.append(
                StrategyEvaluation(
                    SetupStatus.REJECTED, "regime_does_not_permit_orb:" + regime.regime.value
                )
            )
            continue
        retests = []
        if config.require_retest:
            prior_breakouts = [
                candidate
                for candidate in prior_bars
                if candidate.timestamp >= range_end and candidate.close > range_high
            ]
            retests = prior_breakouts[-config.retest_window_bars :]
            if not retests or bar.low > range_high or bar.close <= range_high:
                output.append(
                    StrategyEvaluation(SetupStatus.REJECTED, "breakout_retest_not_confirmed")
                )
                continue
        quote = (quotes or {}).get(bar.timestamp)
        quote_ok, quote_status, spread_bps = _quote_quality(
            quote,
            decision_at,
            config.max_spread_bps,
            config.require_quotes,
            timedelta(minutes=config.max_quote_age_minutes),
        )
        if not quote_ok:
            output.append(StrategyEvaluation(SetupStatus.REJECTED, quote_status))
            continue
        trigger = bar.high + instrument.tick_size
        stop = min(
            range_low - instrument.tick_size, trigger - point.atr14 * config.stop_atr_multiple
        )
        expiry = min(decision_at + timedelta(minutes=config.expiry_minutes), session_close)
        if expiry <= decision_at:
            output.append(
                StrategyEvaluation(SetupStatus.REJECTED, "insufficient_session_time_for_candidate")
            )
            continue
        extra = (
            (
                ("opening_range_high", str(range_high)),
                ("opening_range_low", str(range_low)),
                ("regime_previous_close", str(prior_close) if prior_close is not None else None),
                ("regime", regime.regime.value),
                ("quote_spread_bps", str(spread_bps) if spread_bps is not None else None),
                ("quote_status", quote_status),
            )
            + _candle_snapshot("reference_5m.", bar)
            + tuple(
                item
                for range_bar in session_bars
                if range_bar.timestamp <= bar.timestamp
                for item in _candle_snapshot(
                    "session_path_5m." + range_bar.timestamp.isoformat() + ".", range_bar
                )
            )
            + tuple(
                item
                for retest_bar in retests
                for item in _candle_snapshot(
                    "prior_breakout_5m." + retest_bar.timestamp.isoformat() + ".", retest_bar
                )
            )
            + _instrument_snapshot(instrument)
            + _quote_snapshot(quote)
            + _config_snapshot(config)
        )
        candidate = make_candidate(
            instrument=instrument,
            strategy_id=ORB_ID,
            strategy_version=config.version,
            decision_timestamp=decision_at,
            reference_candle_timestamp=bar.timestamp,
            entry_trigger_type="stop_above_breakout_candle_high",
            entry_trigger_price=trigger,
            stop_price=stop,
            invalidation_price=range_low,
            target_price=trigger + (trigger - stop) * config.target_reward_to_risk,
            expiry_timestamp=expiry,
            market_regime=regime.regime,
            indicator_snapshot=_snapshot(("5m.", point), extra=extra),
            data_quality_status="valid_quotes_" + quote_status,
            replay_id=replay_id,
            configuration_hash=config_hash,
            cost_estimate=cost_estimator.estimate(
                entry=trigger,
                exit_price=trigger + (trigger - stop) * config.target_reward_to_risk,
                instrument_id=instrument.instrument_id,
            ),
        )
        result = registry.emit(
            candidate, now=decision_at, scope=f"{ORB_ID}:{instrument.instrument_id}:{session_key}"
        )
        emitted_result_for_session[session_key] = result
        output.append(result)
    return tuple(output)


def evaluate_range_mean_reversion(*args, **kwargs) -> tuple[StrategyEvaluation, ...]:
    """Explicitly disabled placeholder: this strategy cannot emit candidates."""
    return (StrategyEvaluation(SetupStatus.REJECTED, "range_mean_reversion_disabled"),)
