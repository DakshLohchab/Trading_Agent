"""Synthetic Nautilus 1.231.0 candidate-to-fill and projection integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tradingagents.intraday.candidates import CandidateStatus, TradeCandidate
from tradingagents.intraday.crypto_exchange import parse_binance_spot_exchange_info
from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotExecutionSpec,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.crypto_nautilus import run_crypto_nautilus_execution
from tradingagents.intraday.models import InstrumentMetadata, MarketBar
from tradingagents.intraday.regimes import MarketRegime

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2025, 1, 2, 0, 5, tzinfo=UTC)
FILTER_PATH = ROOT / "tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-raw.json"
FILTERS = parse_binance_spot_exchange_info(
    FILTER_PATH.read_bytes(),
    retrieved_at=datetime.fromisoformat("2026-11-02T17:07:33.792021+00:00"),
)


def setup(**account_overrides):
    instrument = InstrumentMetadata(
        "BTCUSDT.BINANCE_SPOT", "BTCUSDT", "BINANCE_SPOT", "crypto_spot", "BTC", "USDT",
        2, D("0.01"), D("0.00001"), "UTC", "24/7", "synthetic-fixture",
        "synthetic software integration test", D("0.00001"), D("5"),
    )
    spec = CryptoSpotExecutionSpec(
        instrument, "USDT", 8, "USDT", "synthetic-fees-v1", "retained current filter fixture",
        FILTERS.retrieved_at_utc, FILTERS,
    )
    account_values = {
        "account_id": "crypto-synthetic-integration",
        "account_currency": "USDT",
        "starting_quote_balance": D("1000"),
        "starting_base_balance": D("0"),
        "starting_base_cost_basis_quote": None,
        "max_risk_per_trade": D("1"),
        "max_daily_loss": D("5"),
        "max_open_positions": 1,
        "max_trades_per_risk_period": 2,
        "risk_period_minutes": 1440,
        "risk_period_timezone": "UTC",
        "max_market_data_age_minutes": 5,
    }
    account_values.update(account_overrides)
    account = CryptoSpotAccountConfig(**account_values)
    fee_config = CryptoSpotFeeConfiguration(
        D("0.0008"), D("0.001"), "USDT", "synthetic research assumption",
        FILTERS.retrieved_at_utc, False, "synthetic-fees-v1",
    )
    costs = CryptoSpotCostAssumptions(
        fee_config.taker_model(), D("0.01"), D("2"), "synthetic research assumptions", False,
        "synthetic-costs-v1",
    )
    return instrument, spec, account, fee_config, costs


def candle(minute, *, open_, high, low, close, volume="10.00000000"):
    start = datetime(2025, 1, 2, 0, minute, tzinfo=UTC)
    return MarketBar(
        start, "BTCUSDT.BINANCE_SPOT", D(str(open_)), D(str(high)), D(str(low)),
        D(str(close)), D(volume), f"synthetic-minute-{minute}", "synthetic fixture", 1,
    )


def candidate(decision_at=NOW, *, entry="101.00", stop="99.00", target="105.00"):
    return TradeCandidate(
        "candidate-synthetic-001", "BTCUSDT.BINANCE_SPOT", "BINANCE_SPOT", "crypto_spot",
        "crypto_vwap_trend_pullback", "crypto-vwap-ohlcv-1.0.0", "LONG", decision_at,
        decision_at - timedelta(minutes=5), "stop_above_reclaim_candle_high", D(entry), D(stop),
        max(D("100.00"), D(stop)), D(target), D("2"), decision_at + timedelta(minutes=5),
        MarketRegime.TRENDING_UP,
        (("confirmation_available_at", decision_at.isoformat()),
         ("vwap_method", "ohlcv_typical_price_approximation"),
         ("strategy_config.crypto_session_anchor_utc", "00:00:00"),
         ("crypto_strategy.price_input", "ohlcv_typical_price=(high+low+close)/3"),
         ("crypto_strategy.volume_input", "binance_kline_base_volume"),
         ("crypto_strategy.candle_completion", "bar_end_timestamp")),
        "synthetic_complete_candle", "synthetic-fixture",
        "synthetic-nautilus-test", "synthetic-config-hash", None, "unavailable", CandidateStatus.VALID,
    )


def test_valid_candidate_is_risk_approved_filled_and_reconciled_by_nautilus():
    instrument, spec, account, fees, costs = setup()
    bars = [
        candle(0, open_=100, high=100.5, low=99.5, close=100),
        candle(1, open_=100, high=100.5, low=99.5, close=100),
        candle(2, open_=100, high=100.5, low=99.5, close=100),
        candle(3, open_=100, high=100.5, low=99.5, close=100),
        candle(4, open_=100, high=100.5, low=99.5, close=100),
        candle(5, open_=100, high=103, low=99.5, close=102),
        candle(6, open_=102, high=106, low=101, close=105),
        candle(7, open_=105, high=106, low=104, close=105),
    ]
    result = run_crypto_nautilus_execution(
        instrument, bars, [candidate()], execution_spec=spec, account=account,
        fee_configuration=fees, costs=costs, run_id="synthetic-run-001",
    )
    replay = run_crypto_nautilus_execution(
        instrument, bars, [candidate()], execution_spec=spec, account=account,
        fee_configuration=fees, costs=costs, run_id="synthetic-run-001",
    )
    assert result["nautilus_version"] == "1.231.0"
    assert result["account_type"] == "CASH"
    assert result["reconciliation"]["passed"] is True
    assert result["reconciliation"]["errors"] == []
    assert [fill["side"] for fill in result["fills"]] == ["BUY", "SELL"], result
    assert all(fill["candidate_id"] == "candidate-synthetic-001" for fill in result["fills"])
    assert all(datetime.fromisoformat(fill["timestamp"]) > NOW for fill in result["fills"])
    assert result["nautilus_balances_locked"] == {"USDT": "0", "BTC": "0"}
    assert result["projected_account"]["reserved_balances"] == {"BTC": "0", "USDT": "0"}
    assert result["cost_status"] == "COST_INCOMPLETE"
    assert result["realistic_net_performance"] is False
    assert result["events"] == replay["events"]
    assert result["fills"] == replay["fills"]
    assert result["nautilus_balances_total"] == replay["nautilus_balances_total"]


def test_insufficient_quote_rejects_candidate_without_submitting_an_order():
    instrument, spec, account, fees, costs = setup(starting_quote_balance=D("1"))
    bars = [candle(0, open_=100, high=100.5, low=99.5, close=100),
            candle(1, open_=100, high=100.5, low=99.5, close=100),
            candle(2, open_=100, high=100.5, low=99.5, close=100),
            candle(3, open_=100, high=100.5, low=99.5, close=100),
            candle(4, open_=100, high=100.5, low=99.5, close=100),
            candle(5, open_=100, high=101, low=99, close=100)]
    result = run_crypto_nautilus_execution(
        instrument, bars, [candidate()], execution_spec=spec, account=account,
        fee_configuration=fees, costs=costs, run_id="synthetic-run-no-cash",
    )
    assert result["fills"] == []
    assert not any(event["type"] in {"order_submit_intent", "order_submitted"} for event in result["events"])
    assert result["rejections"] == [{"candidate_id": "candidate-synthetic-001", "reason": "below_minimum_notional"}]


def test_percent_price_filter_rejects_out_of_band_candidate_before_order_creation():
    instrument, spec, account, fees, costs = setup()
    bars = [candle(index, open_=100, high=100.5, low=99.5, close=100) for index in range(6)]
    result = run_crypto_nautilus_execution(
        instrument, bars, [candidate(entry="130.00", stop="128.00", target="140.00")],
        execution_spec=spec, account=account, fee_configuration=fees, costs=costs,
        run_id="synthetic-percent-filter",
    )
    assert result["fills"] == []
    assert result["rejections"] == [
        {"candidate_id": "candidate-synthetic-001", "reason": "percent_price_by_side_filter_violation"}
    ]


def test_duplicate_candidate_and_missing_cost_inputs_fail_before_nautilus():
    instrument, spec, account, fees, costs = setup()
    bars = [candle(4, open_=100, high=101, low=99, close=100)]
    with pytest.raises(ValueError, match="duplicate candidate IDs"):
        run_crypto_nautilus_execution(
            instrument, bars, [candidate(), candidate()], execution_spec=spec, account=account,
            fee_configuration=fees, costs=costs, run_id="synthetic-duplicate",
        )
    with pytest.raises(ValueError, match="fee, spread, and slippage"):
        run_crypto_nautilus_execution(
            instrument, bars, [candidate()], execution_spec=spec, account=account,
            fee_configuration=fees, costs=None, run_id="synthetic-missing-costs",
        )
