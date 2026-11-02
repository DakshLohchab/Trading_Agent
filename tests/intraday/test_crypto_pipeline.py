"""Synthetic completed-market-data to strategy-candidate pipeline tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from tradingagents.intraday.crypto_pipeline import evaluate_crypto_market_data
from tradingagents.intraday.models import InstrumentMetadata, MarketBar


def test_one_minute_crypto_pipeline_does_not_use_incomplete_15m_confirmation():
    instrument = InstrumentMetadata(
        "BTCUSDT.BINANCE_SPOT", "BTCUSDT", "BINANCE_SPOT", "crypto_spot", "BTC", "USDT",
        2, D("0.01"), D("0.00001"), "UTC", "24/7", "synthetic-fixture",
        "tests/intraday/test_crypto_pipeline.py", D("0.00001"), D("5"),
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    market_bars = [
        MarketBar(
            start + timedelta(minutes=index), instrument.instrument_id,
            D("100"), D("101"), D("99"), D("100"), D("1"),
            f"synthetic-1m-{index}", "synthetic software fixture", 1,
        )
        for index in range(5)
    ]
    batch = evaluate_crypto_market_data(instrument, market_bars, replay_id="synthetic-pipeline")
    assert len(batch.candles_5m) == 1
    assert batch.candles_5m[0].is_valid
    assert batch.candles_5m[0].end_timestamp == start + timedelta(minutes=5)
    assert batch.candles_15m == ()
    assert batch.evaluations[0].reason == "higher_timeframe_not_yet_complete"
    assert batch.candidates == ()
