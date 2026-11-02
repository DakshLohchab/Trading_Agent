"""Completed BTC Spot OHLCV -> indicators -> crypto VWAP candidates pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from tradingagents.intraday.aggregation import aggregate_bars
from tradingagents.intraday.indicators import compute_indicators
from tradingagents.intraday.models import InstrumentMetadata, MarketBar
from tradingagents.intraday.strategies import (
    CryptoVWAPPullbackConfig,
    evaluate_crypto_vwap_trend_pullback,
)


@dataclass(frozen=True, slots=True)
class CryptoCandidateBatch:
    candles_5m: tuple
    candles_15m: tuple
    indicators_5m: tuple
    indicators_15m: tuple
    evaluations: tuple
    candidates: tuple


def evaluate_crypto_market_data(
    instrument: InstrumentMetadata,
    bars_1m: tuple[MarketBar, ...] | list[MarketBar],
    *,
    replay_id: str,
    config: CryptoVWAPPullbackConfig | None = None,
) -> CryptoCandidateBatch:
    """Build only from complete 1m source bars; 5m/15m availability is bar-end based."""
    selected = config or CryptoVWAPPullbackConfig()
    if instrument.asset_class != "crypto_spot" or instrument.session_calendar != "24/7":
        raise ValueError("crypto candidate pipeline requires a 24/7 spot instrument")
    if not bars_1m or any(
        item.instrument_id != instrument.instrument_id
        or item.base_interval_minutes != 1
        or item.quality != "observed"
        for item in bars_1m
    ):
        raise ValueError("crypto candidate pipeline requires observed 1m source bars")
    candles_5m = aggregate_bars(bars_1m, instrument, 5)
    candles_15m = aggregate_bars(bars_1m, instrument, 15)
    indicators_5m = compute_indicators(
        candles_5m,
        instrument,
        crypto_session_anchor_utc=selected.utc_anchor,
    )
    indicators_15m = compute_indicators(
        candles_15m,
        instrument,
        crypto_session_anchor_utc=selected.utc_anchor,
    )
    evaluations = evaluate_crypto_vwap_trend_pullback(
        instrument,
        candles_5m,
        indicators_5m,
        candles_15m,
        indicators_15m,
        replay_id=replay_id,
        config=selected,
    )
    candidates = tuple(item.candidate for item in evaluations if item.candidate is not None)
    return CryptoCandidateBatch(
        tuple(candles_5m), tuple(candles_15m), tuple(indicators_5m), tuple(indicators_15m),
        tuple(evaluations), candidates,
    )
