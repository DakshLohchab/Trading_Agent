from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from tradingagents.intraday.candidates import make_candidate
from tradingagents.intraday.costs import UnavailableCostEstimator
from tradingagents.intraday.execution import (
    EstimatedAdValoremCosts,
    ExecutionLedger,
    RiskContext,
    RiskLimits,
    approve_candidate,
)
from tradingagents.intraday.metrics import ClosedTrade, EquityMark, calculate_metrics
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata
from tradingagents.intraday.nautilus_execution import run_nautilus_execution
from tradingagents.intraday.regimes import MarketRegime

D0 = datetime(2026, 11, 2, 3, 45, tzinfo=UTC)


def instrument(*, min_notional=None):
    return InstrumentMetadata(
        "TCS.NSE",
        "TCS",
        "NSE",
        "equity",
        "INR",
        "INR",
        2,
        D("0.05"),
        D("1"),
        "Asia/Kolkata",
        "NSE",
        "synthetic-fixture",
        "tests/intraday/test_execution.py",
        min_order_quantity=D("1"),
        min_order_notional=min_notional,
    )


def costs():
    return EstimatedAdValoremCosts(
        brokerage_rate=D("0.0001"),
        exchange_rate=D("0.0001"),
        regulatory_rate=D("0.00001"),
        transaction_tax_buy_rate=D("0"),
        transaction_tax_sell_rate=D("0.0002"),
        stamp_duty_buy_rate=D("0.0001"),
        gst_rate=D("0.18"),
    )


def candidate(inst=None, *, decision=D0 + timedelta(minutes=5)):
    inst = inst or instrument()
    return make_candidate(
        instrument=inst,
        strategy_id="vwap_trend_pullback",
        strategy_version="1.0.0",
        decision_timestamp=decision,
        reference_candle_timestamp=decision - timedelta(minutes=5),
        entry_trigger_type="stop_above_reclaim_candle_high",
        entry_trigger_price=D("100.50"),
        stop_price=D("99.50"),
        invalidation_price=D("99.55"),
        target_price=D("102.50"),
        expiry_timestamp=decision + timedelta(minutes=30),
        market_regime=MarketRegime.TRENDING_UP,
        indicator_snapshot=(("close", "100"),),
        data_quality_status="valid_quotes_unavailable",
        replay_id="synthetic-replay",
        configuration_hash="a" * 64,
        cost_estimate=UnavailableCostEstimator().estimate(
            entry=D("100.5"), exit_price=D("102.5"), instrument_id=inst.instrument_id
        ),
    )


def test_risk_approval_rounds_down_and_includes_fee_and_two_sided_slippage():
    decision = approve_candidate(
        candidate(),
        instrument(),
        RiskContext(D("10000")),
        RiskLimits(),
        costs(),
        estimated_slippage_per_unit=D("0.05"),
    )
    assert decision.approved
    assert decision.quantity == decision.quantity.to_integral_value(rounding="ROUND_FLOOR")
    assert decision.max_loss_at_stop <= D("25")
    assert decision.estimated_entry_cost > 0 and decision.estimated_exit_cost > 0
    assert len(decision.risk_config_hash) == 64


@pytest.mark.parametrize(
    ("context", "inst", "expected"),
    [
        (RiskContext(D("0")), None, "insufficient_cash"),
        (RiskContext(D("10000"), open_positions=1), None, "concurrent_position_limit"),
        (RiskContext(D("10000"), trades_this_session=3), None, "session_trade_limit"),
        (RiskContext(D("10000"), realized_pnl_today=D("-75")), None, "daily_loss_limit_exhausted"),
        (RiskContext(D("10000")), "USD", "currency_conversion_unavailable"),
    ],
)
def test_risk_gate_rejections(context, inst, expected):
    item = instrument()
    if inst == "USD":
        item = InstrumentMetadata(
            "ABC.XNYS",
            "ABC",
            "NYSE",
            "equity",
            "USD",
            "USD",
            2,
            D("0.01"),
            D("1"),
            "America/New_York",
            "NYSE",
            "fixture",
            "test",
        )
    decision = approve_candidate(
        candidate(item), item, context, RiskLimits(), costs(), estimated_slippage_per_unit=D("0.05")
    )
    assert not decision.approved and decision.reason == expected


def test_minimum_notional_and_remaining_daily_loss_reduce_or_reject_size():
    decision = approve_candidate(
        candidate(instrument(min_notional=D("100000"))),
        instrument(min_notional=D("100000")),
        RiskContext(D("10000")),
        RiskLimits(),
        costs(),
        estimated_slippage_per_unit=D("0.05"),
    )
    assert not decision.approved and decision.reason == "below_minimum_notional"
    limited = approve_candidate(
        candidate(),
        instrument(),
        RiskContext(D("10000"), realized_pnl_today=D("-70")),
        RiskLimits(),
        costs(),
        estimated_slippage_per_unit=D("0.05"),
    )
    assert limited.approved and limited.max_loss_at_stop <= D("5")


def test_execution_ledger_is_separate_append_only_and_ordered(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.sqlite")
    ledger.append(
        run_id="r1",
        event_type="candidate_received",
        timestamp=D0,
        payload={"candidate": "id"},
        candidate_id="id",
    )
    ledger.append(
        run_id="r1",
        event_type="risk_approval",
        timestamp=D0 + timedelta(seconds=1),
        payload={"approved": True},
        candidate_id="id",
    )
    records = ledger.events("r1")
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[1]["payload"]["approved"] is True
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.append(run_id="r1", event_type="bad", timestamp=datetime(2026, 1, 1), payload={})


def test_performance_metrics_use_net_pnl_and_close_equity_marks():
    trades = [
        ClosedTrade(
            "vwap", D0, D0 + timedelta(minutes=10), D("1000"), D("1020"), D("40"), D("2"), D("1")
        ),
        ClosedTrade(
            "orb", D0, D0 + timedelta(minutes=15), D("800"), D("780"), D("-20"), D("2"), D("1")
        ),
    ]
    marks = [
        EquityMark(D0 + timedelta(hours=7), D("10017"), D("10017"), D("0")),
        EquityMark(D0 + timedelta(days=1, hours=7), D("10034"), D("10034"), D("0")),
    ]
    metrics = calculate_metrics(starting_capital=D("10000"), trades=trades, equity_marks=marks)
    assert metrics.net_pnl == D("14")
    assert metrics.trade_count == 2 and metrics.winning_trades == 1 and metrics.losing_trades == 1
    assert metrics.ending_equity == D("10034")
    assert metrics.maximum_drawdown == 0
    assert metrics.annualization.startswith("daily close returns")


def test_nautilus_121_simulation_uses_next_event_and_owns_fills_cash_and_position(tmp_path):
    inst = instrument()
    prices = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "102", "100", "101"),
        ("101", "103", "100.5", "102.5"),
        ("102.5", "102.5", "102", "102"),
    ]
    bars = []
    for index, (opening, high, low, close) in enumerate(prices):
        start = D0 + timedelta(minutes=index * 5)
        bars.append(
            AggregatedCandle(
                start,
                start + timedelta(minutes=5),
                inst.instrument_id,
                5,
                D(opening),
                D(high),
                D(low),
                D(close),
                D("1000"),
                True,
                source_ids=(f"fixture-{index}",),
            )
        )
    candidate_item = candidate(decision=bars[0].end_timestamp)
    session_close = datetime(2026, 11, 2, 10, tzinfo=UTC)
    result = run_nautilus_execution(
        inst,
        bars,
        [candidate_item, candidate_item],
        risk_limits=RiskLimits(),
        costs=costs(),
        session_closes={bar.end_timestamp: session_close for bar in bars},
        run_id="nautilus-smoke",
        estimated_slippage_per_unit=D("0.05"),
        ledger_path=tmp_path / "execution.sqlite",
    )
    assert result["nautilus_version"] == "1.231.0"
    assert result["fills"]
    entry_fill = next(fill for fill in result["fills"] if fill["role"] == "entry")
    assert datetime.fromisoformat(entry_fill["timestamp"]) > candidate_item.decision_timestamp
    assert any(event["event_type"] == "risk_approval" for event in result["events"])
    assert any(
        event["event_type"] == "risk_rejection" and event.get("reason") == "duplicate_candidate"
        for event in result["events"]
    )
    assert any(fill["role"] == "exit" for fill in result["fills"])
    assert result["equity_marks"] and result["account_currency"] == "INR"
    assert result["cost_completeness"].startswith("costs_incomplete")
    assert not result["open_position_at_end"]
    assert result["performance"]["by_strategy"]["vwap_trend_pullback"]["trade_count"] == 1
    assert result["performance"]["by_market_regime_trade_totals"]["TRENDING_UP"]["trade_count"] == 1
    assert result["performance"]["by_entry_hour_local_trade_totals"][9]["trade_count"] == 1
    ledger = ExecutionLedger(tmp_path / "execution.sqlite")
    assert {event["event_type"] for event in ledger.events("nautilus-smoke")} >= {
        "trade_candidate",
        "risk_approval",
        "order_submission_intent",
        "completed_fill",
        "cash_movement",
        "portfolio_snapshot",
        "run_completed",
    }
    repeated = run_native_fixture(
        tmp_path,
        prices,
        candidate_item,
        "nautilus-smoke-repeat",
        session_close,
    )
    assert repeated["fills"] == result["fills"]
    assert repeated["equity_marks"] == result["equity_marks"]


def run_native_fixture(
    tmp_path, price_rows, candidate_item, run_id, close_timestamp, *, volume="1000"
):
    inst = instrument()
    bars = []
    for index, (opening, high, low, close) in enumerate(price_rows):
        start = D0 + timedelta(minutes=index * 5)
        bars.append(
            AggregatedCandle(
                start,
                start + timedelta(minutes=5),
                inst.instrument_id,
                5,
                D(opening),
                D(high),
                D(low),
                D(close),
                D(volume),
                True,
                source_ids=(f"{run_id}-{index}",),
            )
        )
    return run_nautilus_execution(
        inst,
        bars,
        [candidate_item],
        risk_limits=RiskLimits(),
        costs=costs(),
        session_closes={bar.end_timestamp: close_timestamp for bar in bars},
        run_id=run_id,
        estimated_slippage_per_unit=D("0.05"),
        ledger_path=tmp_path / f"{run_id}.sqlite",
    )


def test_stop_wins_ohlcv_collision_and_gap_fill_uses_worse_available_price(tmp_path):
    candidate_item = candidate()
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "102", "100", "101"),
        ("98", "103", "97", "100"),  # Both stop and target touched; open gapped through stop.
        ("99", "100", "98", "99"),
    ]
    result = run_native_fixture(
        tmp_path,
        rows,
        candidate_item,
        "stop-collision",
        datetime(2026, 11, 2, 10, tzinfo=UTC),
    )
    stop_fill = next(fill for fill in result["fills"] if fill["role"] == "protective_stop")
    assert D(stop_fill["price"]) <= candidate_item.stop_price
    assert not any(
        event.get("modification") == "target_touched_next_event_market_exit"
        for event in result["events"]
    )
    assert result["closed_trades"]


def test_pending_entry_expires_and_open_position_is_closed_before_session_end(tmp_path):
    short_lived = replace(candidate(), expiry_timestamp=D0 + timedelta(minutes=15))
    no_breakout = [("100", "100.25", "99.75", "100")] * 6
    expiry_result = run_native_fixture(
        tmp_path,
        no_breakout,
        short_lived,
        "entry-expiry",
        D0 + timedelta(minutes=30),
    )
    assert any(event["event_type"] == "order_expiry" for event in expiry_result["events"])
    assert not expiry_result["fills"]

    long_lived = replace(candidate(), expiry_timestamp=D0 + timedelta(hours=1))
    quiet_after_entry = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "102", "100", "101"),
        ("101", "101.5", "100.5", "101"),
        ("101", "101.5", "100.5", "101"),
    ]
    session_close = D0 + timedelta(minutes=30)
    close_result = run_native_fixture(
        tmp_path,
        quiet_after_entry,
        long_lived,
        "forced-close",
        session_close,
    )
    assert any(
        event.get("modification") == "same_session_forced_exit" for event in close_result["events"]
    )
    assert not close_result["open_position_at_end"]


def test_bar_liquidity_partial_fill_and_remainder_cancellation_are_recorded(tmp_path):
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "102", "100", "101"),
        ("101", "103", "100.5", "102.5"),
        ("102.5", "102.5", "102", "102"),
    ]
    result = run_native_fixture(
        tmp_path,
        rows,
        candidate(),
        "partial-liquidity",
        datetime(2026, 11, 2, 10, tzinfo=UTC),
        volume="1",
    )
    assert any(event["event_type"] == "partial_fill" for event in result["events"])
    assert any(event["event_type"] == "order_cancellation" for event in result["events"])
