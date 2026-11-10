"""Synthetic software fixtures for the isolated BTC/USDT spot boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from tradingagents.intraday.candidates import CandidateStatus, TradeCandidate
from tradingagents.intraday.crypto_costs import CryptoExecutionCostModel
from tradingagents.intraday.crypto_execution import (
    CryptoMarketDataState,
    CryptoRiskError,
    CryptoSpotAccountConfig,
    CryptoSpotAccountLedger,
    CryptoSpotCostAssumptions,
    CryptoSpotExecutionSpec,
    CryptoSpotFeeModel,
    approve_crypto_candidate,
)
from tradingagents.intraday.models import InstrumentMetadata
from tradingagents.intraday.regimes import MarketRegime

NOW = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)


def instrument(**overrides):
    values = {
        "instrument_id": "BTCUSDT.BINANCE_SPOT", "symbol": "BTCUSDT", "venue": "BINANCE_SPOT",
        "asset_class": "crypto_spot", "base_currency": "BTC", "quote_currency": "USDT",
        "price_precision": 2, "tick_size": D("0.01"), "quantity_increment": D("0.001"),
        "exchange_timezone": "UTC", "session_calendar": "24/7", "data_source": "binance-public-data",
        "provenance": "synthetic test fixture", "min_order_quantity": D("0.001"),
        "min_order_notional": D("10"),
    }
    values.update(overrides)
    return InstrumentMetadata(**values)


def spec(inst=None, *, fee_currency="USDT"):
    inst = inst or instrument()
    return CryptoSpotExecutionSpec(inst, "USDT", 3, fee_currency, "test-fees-v1",
                                   "synthetic software-test exchange-filter fixture", NOW)


def account(**overrides):
    values = {
        "account_id": "crypto-btcusdt-test", "account_currency": "USDT",
        "starting_quote_balance": D("1000"), "starting_base_balance": D("0"),
        "starting_base_cost_basis_quote": None, "max_risk_per_trade": D("10"),
        "max_daily_loss": D("30"), "max_open_positions": 1,
        "max_trades_per_risk_period": 3, "risk_period_minutes": 1440,
        "risk_period_timezone": "UTC", "max_market_data_age_minutes": 5,
    }
    values.update(overrides)
    return CryptoSpotAccountConfig(**values)


def fees(currency="USDT", rate="0.001"):
    return CryptoSpotFeeModel(D(rate), currency, "synthetic test assumption", "test-fees-v1", False)


def costs(model=None):
    model = model or fees()
    return CryptoSpotCostAssumptions(model, D("0.01"), D("2"), "synthetic test assumptions", False, "test-costs-v1")


def candidate(inst=None, *, entry="101.00", stop="99.00", status=CandidateStatus.VALID):
    inst = inst or instrument()
    return TradeCandidate(
        "candidate-1", inst.instrument_id, inst.venue, "crypto_spot", "crypto_vwap_trend_pullback", "1.0.0",
        "LONG", NOW, NOW - timedelta(minutes=5), "stop_above_reclaim_candle_high",
        D(entry), D(stop), D("100.00"), D("105.00"), D("2"), NOW + timedelta(minutes=15),
        MarketRegime.TRENDING_UP, (), "valid_test_data", inst.data_source, "replay-test", "config-hash",
        None, "unavailable", status,
    )


def test_crypto_instrument_spec_requires_explicit_spot_metadata_and_quote_account():
    assert spec().instrument.instrument_id == "BTCUSDT.BINANCE_SPOT"
    with pytest.raises(CryptoRiskError, match="quote currency"):
        CryptoSpotExecutionSpec(instrument(), "INR", 3, "USDT", "fees-v1", "source", NOW)
    with pytest.raises(CryptoRiskError, match="minimum quantity"):
        spec(instrument(min_order_notional=None))


def test_buy_and_sell_account_in_usdt_tracks_btc_basis_and_quote_fees():
    ledger = CryptoSpotAccountLedger(spec(), account())
    buy_fee = fees("USDT")
    ledger.reserve_order("buy-1", "BUY", D("0.5"), D("1000"), submitted_at=NOW, fee_model=buy_fee)
    assert ledger.available_balance("USDT") == D("499.5000")
    buy = ledger.apply_fill("buy-1", D("0.5"), D("1000"), timestamp=NOW + timedelta(minutes=1))
    assert buy.fee_amount == D("0.5000") and buy.fee_currency == "USDT"
    assert ledger.balances == {"USDT": D("499.5000"), "BTC": D("0.5")}
    assert ledger.unrealized_pnl_quote(D("1000")) == D("-0.5000")
    ledger.reserve_order("sell-1", "SELL", D("0.2"), D("1100"), submitted_at=NOW + timedelta(minutes=1), fee_model=buy_fee)
    sell = ledger.apply_fill("sell-1", D("0.2"), D("1100"), timestamp=NOW + timedelta(minutes=2))
    assert sell.realized_pnl_quote == D("19.580000")
    assert ledger.balances == {"USDT": D("719.2800"), "BTC": D("0.3")}
    assert ledger.fees_paid["USDT"] == D("0.7200")


def test_ledger_replay_is_deterministic():
    def replay():
        ledger = CryptoSpotAccountLedger(spec(), account())
        model = fees()
        ledger.reserve_order("buy", "BUY", D("0.2"), D("1000"), submitted_at=NOW, fee_model=model)
        ledger.apply_fill("buy", D("0.2"), D("1000"), timestamp=NOW + timedelta(minutes=1))
        ledger.reserve_order(
            "sell", "SELL", D("0.1"), D("1050"),
            submitted_at=NOW + timedelta(minutes=1), fee_model=model,
        )
        ledger.apply_fill("sell", D("0.1"), D("1050"), timestamp=NOW + timedelta(minutes=2))
        return ledger.snapshot(mark_price=D("1050")), ledger.events, ledger.fills

    assert replay() == replay()


def test_base_currency_fee_debits_btc_inventory():
    ledger = CryptoSpotAccountLedger(spec(fee_currency="BTC"), account())
    model = fees("BTC")
    ledger.reserve_order("buy", "BUY", D("0.5"), D("1000"), submitted_at=NOW, fee_model=model)
    fill = ledger.apply_fill("buy", D("0.5"), D("1000"), timestamp=NOW + timedelta(seconds=1))
    assert fill.fee_amount == D("0.0005")
    assert ledger.balances["BTC"] == D("0.4995")
    assert ledger.fees_paid["BTC"] == D("0.0005")
    ledger.reserve_order("sell", "SELL", D("0.2"), D("1100"), submitted_at=NOW + timedelta(seconds=1), fee_model=model)
    ledger.apply_fill("sell", D("0.2"), D("1100"), timestamp=NOW + timedelta(seconds=2))
    assert ledger.balances["BTC"] == D("0.2993")
    assert ledger.balances["USDT"] == D("720.0")
    assert ledger.fees_paid["BTC"] == D("0.0007")


@pytest.mark.parametrize(
    ("side", "quantity", "expected"),
    [("BUY", "2", "insufficient available USDT"), ("SELL", "0.1", "insufficient available BTC")],
)
def test_insufficient_balances_reject_without_borrowing(side, quantity, expected):
    ledger = CryptoSpotAccountLedger(spec(), account(starting_quote_balance=D("50")))
    with pytest.raises(CryptoRiskError, match=expected):
        ledger.reserve_order("order", side, D(quantity), D("1000"), submitted_at=NOW, fee_model=fees())
    assert ledger.balances["BTC"] >= 0 and ledger.balances["USDT"] >= 0


def test_precision_minimum_notional_and_missing_fee_model_fail_closed():
    ledger = CryptoSpotAccountLedger(spec(), account())
    with pytest.raises(CryptoRiskError, match="quantity step"):
        ledger.reserve_order("bad-step", "BUY", D("0.0005"), D("1000"), submitted_at=NOW, fee_model=fees())
    with pytest.raises(CryptoRiskError, match="minimum"):
        ledger.reserve_order("small", "BUY", D("0.001"), D("1000"), submitted_at=NOW, fee_model=fees())
    with pytest.raises(CryptoRiskError, match="price tick"):
        ledger.reserve_order("bad-tick", "BUY", D("0.1"), D("1000.001"), submitted_at=NOW, fee_model=fees())
    with pytest.raises(CryptoRiskError, match="fee model is required"):
        ledger.reserve_order("no-fees", "BUY", D("0.1"), D("1000"), submitted_at=NOW, fee_model=None)


def test_fills_must_follow_submission_and_cancel_releases_reservations():
    ledger = CryptoSpotAccountLedger(spec(), account())
    ledger.reserve_order("buy", "BUY", D("0.2"), D("1000"), submitted_at=NOW, fee_model=fees())
    with pytest.raises(CryptoRiskError, match="later market-data event"):
        ledger.apply_fill("buy", D("0.1"), D("1000"), timestamp=NOW)
    ledger.cancel_order("buy")
    assert ledger.available_balance("USDT") == D("1000")
    assert ledger.reserved_balances["USDT"] == 0


def test_failed_fill_does_not_mutate_balances_or_reservations():
    ledger = CryptoSpotAccountLedger(spec(), account(starting_quote_balance=D("550")))
    ledger.reserve_order("buy", "BUY", D("0.5"), D("1000"), submitted_at=NOW, fee_model=fees())
    before = (dict(ledger.balances), dict(ledger.reserved_balances))
    with pytest.raises(CryptoRiskError, match="overdraw USDT"):
        ledger.apply_fill("buy", D("0.5"), D("1200"), timestamp=NOW + timedelta(minutes=1))
    assert (ledger.balances, ledger.reserved_balances) == before


def test_crypto_risk_gate_checks_cost_fx_freshness_and_period_limits():
    inst, cfg, model = instrument(), account(), fees()
    candidate_item = candidate(inst)
    data = CryptoMarketDataState(NOW, True, "completed 5m candle hash")
    decision = approve_crypto_candidate(
        candidate_item, spec(inst), cfg, available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=data, now=NOW, realized_pnl_this_period=D(0),
        unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
    )
    assert decision.approved and decision.quantity % inst.quantity_increment == 0
    assert decision.cost_status == "COST_INCOMPLETE"
    breakdown = decision.cost_breakdown
    shared_model = CryptoExecutionCostModel(costs(model).spread_bps, costs(model).slippage_bps)
    assert breakdown.reference_entry_price == candidate_item.entry_trigger_price
    assert breakdown.adjusted_expected_entry_price == shared_model.adjusted_price(
        candidate_item.entry_trigger_price, "BUY", inst.tick_size
    )
    assert breakdown.adjusted_expected_exit_price == shared_model.adjusted_price(
        candidate_item.stop_price, "SELL", inst.tick_size
    )
    assert breakdown.estimated_total_loss_after_costs == decision.modeled_loss_quote
    assert breakdown.estimated_entry_fee == decision.quantity * breakdown.adjusted_expected_entry_price * model.fee_rate
    missing_fee = approve_crypto_candidate(
        candidate_item, spec(inst), cfg, available_quote_balance=D("1000"), fee_model=None,
        costs=None, market_data=data, now=NOW, realized_pnl_this_period=D(0),
        unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
    )
    assert not missing_fee.approved and missing_fee.reason == "fee_or_execution_cost_model_missing"
    no_fx = approve_crypto_candidate(
        candidate_item, spec(inst), account(account_currency="INR"), available_quote_balance=D("1000"),
        fee_model=model, costs=costs(model), market_data=data, now=NOW,
        realized_pnl_this_period=D(0), unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
    )
    assert not no_fx.approved and no_fx.reason == "account_currency_mismatch_fx_unavailable"
    future = approve_crypto_candidate(
        candidate_item, spec(inst), cfg, available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=CryptoMarketDataState(NOW + timedelta(seconds=1), True, "bad"),
        now=NOW, realized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
        unrealized_pnl_this_period=D(0),
    )
    assert not future.approved and future.reason == "future_or_incomplete_candle"
    limited = approve_crypto_candidate(
        candidate_item, spec(inst), cfg, available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=data, now=NOW, realized_pnl_this_period=D("-30"),
        unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
    )
    assert not limited.approved and limited.reason == "daily_loss_limit_exhausted"
    integrity = approve_crypto_candidate(
        candidate_item, spec(inst), cfg, available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=CryptoMarketDataState(NOW, False, "bad"),
        now=NOW, realized_pnl_this_period=D(0), unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=0,
    )
    assert not integrity.approved and integrity.reason == "market_data_integrity_failed"
    position_limited = approve_crypto_candidate(
        candidate_item, spec(inst), account(), available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=data, now=NOW, realized_pnl_this_period=D(0),
        unrealized_pnl_this_period=D(0), open_positions=1, trades_this_period=0,
    )
    assert not position_limited.approved and position_limited.reason == "open_position_limit"
    trade_limited = approve_crypto_candidate(
        candidate_item, spec(inst), account(), available_quote_balance=D("1000"), fee_model=model,
        costs=costs(model), market_data=data, now=NOW, realized_pnl_this_period=D(0),
        unrealized_pnl_this_period=D(0), open_positions=0, trades_this_period=3,
    )
    assert not trade_limited.approved and trade_limited.reason == "risk_period_trade_limit"


def test_cost_model_does_not_claim_user_assumptions_as_account_specific():
    assert costs().cost_status == "COST_INCOMPLETE"
    verified = CryptoSpotFeeModel(D("0.001"), "USDT", "account-specific source", "account-v1", True)
    assert CryptoSpotCostAssumptions(verified, D("0.01"), D("2"), "verified source", True, "v1").cost_status == "COST_COMPLETE"
