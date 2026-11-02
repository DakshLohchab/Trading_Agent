"""Interpretable deterministic market-regime classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from tradingagents.intraday.indicators import IndicatorPoint

D = Decimal


class MarketRegime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    DISLOCATED = "DISLOCATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    adx_trend_min: Decimal = D("20")
    high_atr_percent: Decimal = D("0.04")
    dislocated_atr_percent: Decimal = D("0.12")
    dislocated_bar_return: Decimal = D("0.08")
    dislocated_relative_volume: Decimal = D("8")

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = D(str(getattr(self, field)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field} must be a finite non-negative decimal")
            object.__setattr__(self, field, value)
        if self.dislocated_atr_percent < self.high_atr_percent:
            raise ValueError("dislocated_atr_percent must be >= high_atr_percent")


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    timestamp: datetime
    regime: MarketRegime
    reasons: tuple[str, ...]
    atr_percent: Decimal | None
    bar_return: Decimal | None


def classify_regime(
    point: IndicatorPoint,
    *,
    previous_close: Decimal | None = None,
    config: RegimeConfig | None = None,
) -> RegimeDecision:
    """Classify one completed bar without using later bars."""
    config = config or RegimeConfig()
    required = (point.close, point.atr14, point.adx14, point.ema20, point.ema50, point.ema20_slope)
    if not point.data_valid or any(value is None for value in required) or point.close == 0:
        return RegimeDecision(
            point.available_at,
            MarketRegime.UNKNOWN,
            ("indicator_warmup_or_invalid_data",),
            None,
            None,
        )
    atr_percent = point.atr14 / abs(point.close)
    bar_return = None
    if previous_close is not None and previous_close != 0:
        bar_return = abs(point.close - previous_close) / abs(previous_close)
    if (
        atr_percent >= config.dislocated_atr_percent
        or (bar_return is not None and bar_return >= config.dislocated_bar_return)
        or (
            point.relative_volume is not None
            and point.relative_volume >= config.dislocated_relative_volume
        )
    ):
        return RegimeDecision(
            point.available_at,
            MarketRegime.DISLOCATED,
            ("dislocation_threshold",),
            atr_percent,
            bar_return,
        )
    if atr_percent >= config.high_atr_percent:
        return RegimeDecision(
            point.available_at,
            MarketRegime.HIGH_VOLATILITY,
            ("atr_percent_threshold",),
            atr_percent,
            bar_return,
        )
    if point.adx14 >= config.adx_trend_min and point.ema20 > point.ema50 and point.ema20_slope > 0:
        return RegimeDecision(
            point.available_at,
            MarketRegime.TRENDING_UP,
            ("adx_ema_alignment_positive_slope",),
            atr_percent,
            bar_return,
        )
    if point.adx14 >= config.adx_trend_min and point.ema20 < point.ema50 and point.ema20_slope < 0:
        return RegimeDecision(
            point.available_at,
            MarketRegime.TRENDING_DOWN,
            ("adx_ema_alignment_negative_slope",),
            atr_percent,
            bar_return,
        )
    return RegimeDecision(
        point.available_at,
        MarketRegime.RANGE_BOUND,
        ("trend_threshold_not_met",),
        atr_percent,
        bar_return,
    )
