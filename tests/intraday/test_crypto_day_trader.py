"""Tests for the live & paper crypto day trading system."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from tradingagents.intraday.candidates import SetupStatus, TradeCandidate
from tradingagents.intraday.crypto_day_trader import CryptoDayTrader, DayTraderState
from tradingagents.intraday.crypto_research import CryptoResearchProfile
from tradingagents.intraday.data_sources.binance_spot import BinanceSpotClient
from tradingagents.intraday.models import MarketBar

D = Decimal


def _make_dummy_filters() -> BinanceSpotFilters:
    from tradingagents.intraday.crypto_exchange import BinanceSpotFilters
    return BinanceSpotFilters(
        symbol="BTCUSDT",
        status="TRADING",
        base_asset="BTC",
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_enabled=True,
        tick_size=D("0.01"),
        min_price=D("0.01"),
        max_price=D("1000000.00"),
        lot_step_size=D("0.00001"),
        lot_min_quantity=D("0.00001"),
        lot_max_quantity=D("9000.00"),
        market_step_size=D("0"),
        market_min_quantity=D("0"),
        market_max_quantity=D("122.91"),
        min_notional=D("5.00"),
        max_notional=D("9000000.00"),
        min_notional_applies_to_market=True,
        percent_price_avg_minutes=5,
        bid_multiplier_down=D("0.5"),
        bid_multiplier_up=D("1.2"),
        ask_multiplier_down=D("0.8"),
        ask_multiplier_up=D("2"),
        retrieved_at_utc=datetime.now(UTC),
        exchange_server_time_utc=datetime.now(UTC),
        source_endpoint="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        raw_response_sha256="0" * 64,
    )


def _make_dummy_profile() -> CryptoResearchProfile:
    return CryptoResearchProfile.from_mapping({
        "schema_version": "btcusdt-research-v1",
        "account": {
            "account_id": "test-daytrading",
            "account_currency": "USDT",
            "starting_capital": "10000.0",
            "max_risk_per_trade": "100.0",
            "max_daily_loss": "300.0",
            "max_concurrent_positions": 1,
            "max_trades_per_utc_day": 5,
            "long_only": True,
            "leverage_enabled": False,
            "borrowing_enabled": False,
            "short_selling_enabled": False,
        },
        "fees": {
            "maker_fee_bps": "7.5",
            "taker_fee_bps": "7.5",
            "fee_currency": "USDT",
            "source": "test-fees",
            "configured_at": "2026-11-01T00:00:00+00:00",
            "classification": "RESEARCH_ASSUMPTION",
        },
        "spread": {
            "full_bid_ask_bps": "1.0",
            "source": "test-spread",
            "classification": "RESEARCH_ASSUMPTION",
        },
        "slippage": {
            "additional_adverse_bps": "1.0",
            "source": "test-slippage",
            "classification": "RESEARCH_ASSUMPTION",
        },
    })


def _make_synthetic_bars(count: int = 120, start_dt: datetime | None = None) -> list[MarketBar]:
    start = start_dt or datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    bars = []
    base_price = D("70000.00")
    for i in range(count):
        t = start + timedelta(minutes=i)
        p = base_price + D(str(i * 2))
        bar = MarketBar(
            timestamp=t,
            instrument_id="BINANCE_SPOT:BTCUSDT",
            open=p,
            high=p + D("5.00"),
            low=p - D("5.00"),
            close=p + D("1.00"),
            volume=D("10.5"),
            source_id=f"test:{i}",
            source_timestamp=str(int(t.timestamp() * 1000)),
            base_interval_minutes=1,
            quality="observed",
        )
        bars.append(bar)
    return bars


@pytest.mark.unit
class TestBinanceSpotClient:
    def test_get_ticker_price_parses_cleanly(self):
        client = BinanceSpotClient(api_key="k", api_secret="s")
        fake_resp = mock.Mock(status_code=200)
        fake_resp.json.return_value = {"symbol": "BTCUSDT", "price": "71250.50"}

        with mock.patch.object(client.session, "request", return_value=fake_resp):
            price = client.get_ticker_price("BTCUSDT")

        assert price == D("71250.50")

    def test_get_recent_klines_converts_to_market_bars(self):
        client = BinanceSpotClient()
        now_ms = 1700000100000
        mock_raw = [
            [1700000000000, "70000.0", "70100.0", "69900.0", "70050.0", "15.0", 1700000059999],
            [1700000060000, "70050.0", "70200.0", "70000.0", "70150.0", "20.0", 1700000119999],
        ]
        fake_resp = mock.Mock(status_code=200)
        fake_resp.json.return_value = mock_raw

        with mock.patch.object(client.session, "request", return_value=fake_resp), \
             mock.patch("time.time", return_value=now_ms / 1000.0):
            bars = client.get_recent_klines("BTCUSDT", limit=2)

        assert len(bars) >= 1
        assert bars[0].close == D("70050.0")
        assert bars[0].instrument_id == "BINANCE_SPOT:BTCUSDT"

    def test_signed_request_generates_valid_hmac(self):
        client = BinanceSpotClient(api_key="test-key", api_secret="test-secret")
        signed = client._sign_params({"symbol": "BTCUSDT", "side": "BUY"})
        assert "timestamp" in signed
        assert "signature" in signed
        assert len(signed["signature"]) == 64  # SHA-256 hex length


@pytest.mark.unit
class TestCryptoDayTrader:
    def test_initialization_with_profile(self):
        profile = _make_dummy_profile()
        client = BinanceSpotClient()
        trader = CryptoDayTrader(symbol="BTCUSDT", mode="paper", profile=profile, client=client, filters=_make_dummy_filters())

        assert trader.symbol == "BTCUSDT"
        assert trader.mode == "paper"
        assert trader.state.current_cash == D("10000.0")
        assert trader.instrument.tick_size == D("0.01")

    def test_scan_produces_consistent_report(self):
        profile = _make_dummy_profile()
        client = BinanceSpotClient()
        trader = CryptoDayTrader(symbol="BTCUSDT", mode="paper", profile=profile, client=client, filters=_make_dummy_filters())

        dummy_bars = _make_synthetic_bars(120)
        with mock.patch.object(trader.client, "get_ticker_price", return_value=D("70500.00")), \
             mock.patch.object(trader, "fetch_today_bars", return_value=dummy_bars):
            res = trader.scan()

        assert res["symbol"] == "BTCUSDT"
        assert res["current_price"] == "70500.00"
        assert "vwap" in res
        assert "setup_status" in res

    def test_trade_lifecycle_paper_execution(self):
        profile = _make_dummy_profile()
        client = BinanceSpotClient()
        trader = CryptoDayTrader(symbol="BTCUSDT", mode="paper", profile=profile, client=client, filters=_make_dummy_filters())

        # Mock candidate creation
        from tradingagents.intraday.candidates import CandidateStatus
        from tradingagents.intraday.regimes import MarketRegime

        now_utc = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)
        candidate = TradeCandidate(
            "cand-test-01",
            trader.instrument.instrument_id,
            trader.instrument.venue,
            "crypto_spot",
            "crypto_vwap_trend_pullback",
            "1.0.0",
            "LONG",
            now_utc,
            now_utc - timedelta(minutes=5),
            "stop_above_reclaim_candle_high",
            D("70000.00"),
            D("69500.00"),
            D("69800.00"),
            D("71000.00"),
            D("2.0"),
            now_utc + timedelta(minutes=15),
            MarketRegime.TRENDING_UP,
            (),
            "valid_test_data",
            trader.instrument.data_source,
            "replay-test",
            "config-hash",
            None,
            "unavailable",
            CandidateStatus.VALID,
        )

        ref_bar = MarketBar(
            timestamp=now_utc,
            instrument_id="BINANCE_SPOT:BTCUSDT",
            open=D("70000.00"),
            high=D("70020.00"),
            low=D("69980.00"),
            close=D("70000.00"),
            volume=D("50.0"),
            source_id="test",
            source_timestamp="123456",
        )

        # 1. Test Trade Approval
        decision = trader._approve_trade(candidate, ref_bar)
        assert decision.approved is True
        assert decision.quantity > D("0")

        # 2. Test Execution / Entry
        exec_res = trader._execute_trade(candidate, decision, ref_bar)
        assert exec_res["action"] == "TRADE_ENTERED"
        assert trader.state.active_trade is not None
        assert trader.state.current_cash < D("10000.0")  # Cash deducted for purchase + fee
        assert trader.state.base_asset_balance == decision.quantity

        # 3. Test Holding (price within SL/TP)
        hold_bar = MarketBar(
            timestamp=now_utc + timedelta(minutes=1),
            instrument_id="BINANCE_SPOT:BTCUSDT",
            open=D("70050.00"),
            high=D("70200.00"),
            low=D("69900.00"),  # Above SL 69500
            close=D("70100.00"), # Below TP 71000
            volume=D("10.0"),
            source_id="test-hold",
            source_timestamp="123457",
        )
        assert trader._check_position_exit(hold_bar) is None
        assert trader.state.active_trade is not None

        # 4. Test Target Exit (price touches Target 71000)
        tp_bar = MarketBar(
            timestamp=now_utc + timedelta(minutes=2),
            instrument_id="BINANCE_SPOT:BTCUSDT",
            open=D("70500.00"),
            high=D("71200.00"),  # Hit target
            low=D("70400.00"),
            close=D("71100.00"),
            volume=D("10.0"),
            source_id="test-tp",
            source_timestamp="123458",
        )
        exit_res = trader._check_position_exit(tp_bar)
        assert exit_res is not None
        assert exit_res["action"] == "TRADE_EXITED"
        assert exit_res["exit_reason"] == "TAKE_PROFIT"
        assert D(exit_res["realized_pnl"]) > D("0")
        assert trader.state.active_trade is None
        assert trader.state.base_asset_balance == D("0")
        assert len(trader.state.trade_history) == 1
