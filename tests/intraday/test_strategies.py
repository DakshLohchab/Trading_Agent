from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest

from tradingagents.intraday.candidates import (
    CandidateRegistry,
    CandidateStatus,
    QuoteSnapshot,
    SetupStatus,
)
from tradingagents.intraday.indicators import (
    IndicatorConfig,
    IndicatorPoint,
    TradePrint,
    VerifiedVWAP,
    VWAPValue,
    compute_indicators,
)
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata
from tradingagents.intraday.regimes import MarketRegime, RegimeConfig, classify_regime
from tradingagents.intraday.strategies import (
    CRYPTO_VWAP_PULLBACK_ID,
    ORB_ID,
    RANGE_MEAN_REVERSION_ENABLED,
    VWAP_PULLBACK_ID,
    CryptoVWAPPullbackConfig,
    OpeningRangeConfig,
    VWAPPullbackConfig,
    evaluate_crypto_vwap_trend_pullback,
    evaluate_opening_range_breakout,
    evaluate_range_mean_reversion,
    evaluate_vwap_trend_pullback,
)


def equity():
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
        "tests/intraday/test_strategies.py",
    )


def crypto():
    return InstrumentMetadata(
        "BTCUSDT.BINANCE",
        "BTCUSDT",
        "BINANCE",
        "crypto_spot",
        "BTC",
        "USDT",
        2,
        D("0.01"),
        D("0.001"),
        "UTC",
        "24/7",
        "synthetic-fixture",
        "tests/intraday/test_strategies.py",
    )


def candle(inst, stamp, *, open_, high, low, close, volume=10, interval=5, valid=True):
    return AggregatedCandle(
        stamp,
        stamp + timedelta(minutes=interval),
        inst.instrument_id,
        interval,
        D(str(open_)) if open_ is not None else None,
        D(str(high)) if high is not None else None,
        D(str(low)) if low is not None else None,
        D(str(close)) if close is not None else None,
        D(str(volume)) if volume is not None else None,
        valid,
        None if valid else "synthetic_invalid",
    )


def linear_bars(inst=None, *, count=50, start=None, interval=5, base=100):
    inst = inst or equity()
    start = start or datetime(2026, 11, 2, 3, 45, tzinfo=UTC)
    return [
        candle(
            inst,
            start + timedelta(minutes=interval * i),
            open_=base + i,
            high=base + i + 1,
            low=base + i - 1,
            close=base + i,
            volume=10,
        )
        for i in range(count)
    ]


def point_for(
    inst,
    bar,
    *,
    close=None,
    vwap=100,
    atr=2,
    adx=30,
    ema20=110,
    ema50=100,
    slope=1,
    rvol=2,
    valid=True,
):
    close_value = bar.close if close is None else D(str(close))
    available = bar.end_timestamp
    vwap_session_key = (
        bar.timestamp.date().isoformat() + "T00:00:00Z"
        if inst.asset_class == "crypto_spot"
        else "2026-11-02"
    )
    return IndicatorPoint(
        inst.instrument_id,
        bar.timestamp,
        available,
        bar.interval_minutes,
        close_value if valid else None,
        D("111") if valid else None,
        D(str(ema20)) if valid else None,
        D(str(ema50)) if valid else None,
        D(str(slope)) if valid else None,
        D("60") if valid else None,
        D("2") if valid else None,
        D("1") if valid else None,
        D("1") if valid else None,
        D(str(atr)) if valid else None,
        D(str(adx)) if valid else None,
        D("100") if valid else None,
        D("110") if valid else None,
        D("90") if valid else None,
        D(str(rvol)) if valid else None,
        VWAPValue(
            D(str(vwap)) if valid else None,
            "ohlcv_typical_price_approximation",
            False,
            vwap_session_key,
        ),
        valid,
        IndicatorConfig().configuration_hash,
        IndicatorConfig().warmup_requirements,
        IndicatorConfig().configuration_snapshot,
    )


def nse_open():
    return datetime(2026, 11, 2, 3, 45, tzinfo=UTC)


def test_indicator_warmups_and_reference_values_for_linear_series():
    inst = equity()
    bars = linear_bars(inst, count=50)
    points = compute_indicators(bars, inst)
    assert points[7].ema9 is None
    assert points[8].ema9 == D(104)  # SMA seed over closes 100..108
    assert points[18].ema20 is None
    assert points[19].ema20 == D("109.5")
    assert points[20].ema20 == D("110.5")
    assert points[49].ema50 == D("124.5")
    assert points[14].rsi14 == D(100)
    assert points[25].macd == D(7)
    assert points[32].macd_signal is None
    assert points[33].macd_signal == D(7)
    assert points[26].adx14 == D(100)
    assert points[13].atr14 == D(2)
    assert points[18].bollinger_middle is None
    assert points[19].bollinger_middle == D("109.5")
    assert points[19].bollinger_upper.quantize(D("0.0001")) == D("121.0326")
    assert points[19].relative_volume is None
    assert points[20].relative_volume == D(1)


def test_session_vwap_resets_and_ohlcv_method_is_labeled_approximation():
    inst = equity()
    first_open = nse_open()
    next_open = datetime(2026, 11, 3, 3, 45, tzinfo=UTC)
    bars = [
        candle(inst, first_open, open_=100, high=102, low=99, close=101, volume=2),
        candle(
            inst,
            first_open + timedelta(minutes=5),
            open_=102,
            high=104,
            low=101,
            close=103,
            volume=2,
        ),
        candle(inst, next_open, open_=200, high=202, low=199, close=201, volume=2),
    ]
    points = compute_indicators(bars, inst)
    assert points[0].vwap.value == D("100.6666666666666666666666667")
    assert points[1].vwap.value == D("101.6666666666666666666666667")
    assert points[2].vwap.value == D("200.6666666666666666666666667")
    assert points[0].vwap.method == "ohlcv_typical_price_approximation"
    assert not points[0].vwap.exact_trade_level


def test_exact_trade_vwap_and_verified_source_vwap_are_distinguished():
    inst = equity()
    start = nse_open()
    bars = [candle(inst, start, open_=100, high=110, low=90, close=100, volume=4)]
    trades = [
        TradePrint(start + timedelta(minutes=1), inst.instrument_id, D(100), D(1), "t1"),
        TradePrint(start + timedelta(minutes=2), inst.instrument_id, D(110), D(3), "t2"),
    ]
    exact = compute_indicators(bars, inst, trades=trades)[0].vwap
    assert exact.value == D("107.5") and exact.exact_trade_level and exact.method == "trade_level"
    verified = compute_indicators(
        bars,
        inst,
        verified_vwap={start: VerifiedVWAP(D("106.25"), "provider-vwap", True, "2026-11-02")},
    )[0].vwap
    assert verified.value == D("106.25") and verified.exact_trade_level
    with pytest.raises(ValueError, match="session key"):
        compute_indicators(
            bars,
            inst,
            verified_vwap={start: VerifiedVWAP(D("106.25"), "provider-vwap", True, "2026-11-03")},
        )
    with pytest.raises(ValueError, match="not both"):
        compute_indicators(bars, inst, trades=trades, verified_vwap={})


def test_vwap_approximation_is_unavailable_for_partial_session_input():
    inst = equity()
    mid_session = nse_open() + timedelta(minutes=30)
    points = compute_indicators(
        [candle(inst, mid_session, open_=100, high=101, low=99, close=100)], inst
    )
    assert points[0].vwap.value is None
    assert points[0].vwap.method == "unavailable_incomplete_session_data"


def test_invalid_candle_resets_indicator_warmup_without_forward_fill():
    inst = equity()
    bars = linear_bars(inst, count=15)
    bars[7] = candle(
        inst,
        bars[7].timestamp,
        open_=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        valid=False,
    )
    points = compute_indicators(bars, inst)
    assert points[7].close is None and not points[7].data_valid
    assert points[8].ema9 is None
    assert points[14].ema9 is None


def test_indicators_reject_out_of_order_and_duplicate_input():
    inst = equity()
    bars = linear_bars(inst, count=3)
    with pytest.raises(ValueError, match="unique and chronological"):
        compute_indicators([bars[1], bars[0]], inst)
    with pytest.raises(ValueError, match="unique and chronological"):
        compute_indicators([bars[0], bars[0]], inst)


def test_regime_classifier_trend_range_high_volatility_dislocation_unknown():
    inst = equity()
    bar = linear_bars(inst, count=1)[0]
    base = point_for(inst, bar, close=200, atr=2, ema20=190, ema50=180, slope=1)
    assert classify_regime(base).regime == MarketRegime.TRENDING_UP
    assert (
        classify_regime(replace(base, ema20=D(180), ema50=D(190), ema20_slope=D(-1))).regime
        == MarketRegime.TRENDING_DOWN
    )
    assert (
        classify_regime(
            replace(base, ema20=D(180), ema50=D(180), ema20_slope=D(0), adx14=D(10))
        ).regime
        == MarketRegime.RANGE_BOUND
    )
    assert classify_regime(replace(base, atr14=D(10))).regime == MarketRegime.HIGH_VOLATILITY
    assert classify_regime(base, previous_close=D(180)).regime == MarketRegime.DISLOCATED
    assert classify_regime(replace(base, data_valid=False)).regime == MarketRegime.UNKNOWN
    assert RegimeConfig().adx_trend_min == D(20)


def vwap_fixture(inst=None):
    inst = inst or equity()
    start = nse_open()
    bars = [
        candle(inst, start, open_=101, high=101.5, low=100.5, close=101, volume=10),
        candle(
            inst,
            start + timedelta(minutes=5),
            open_=102,
            high=102.2,
            low=100.2,
            close=101,
            volume=12,
        ),
        candle(
            inst,
            start + timedelta(minutes=10),
            open_=101.5,
            high=103,
            low=100.5,
            close=102.5,
            volume=18,
        ),
    ]
    points = [
        point_for(
            inst, bar, close=bar.close, vwap=100, atr=2, ema20=103, ema50=101, slope=1, rvol=2
        )
        for bar in bars
    ]
    # 15m candle is first available at 09:30 IST; the later candle must not leak backward.
    confirmation_bar = candle(inst, start, open_=102, high=104, low=101, close=103, interval=15)
    future_confirmation_bar = candle(
        inst, start + timedelta(minutes=15), open_=103, high=110, low=102, close=109, interval=15
    )
    confirmations = [
        point_for(inst, confirmation_bar, close=103, ema20=102, ema50=100, slope=1),
        point_for(inst, future_confirmation_bar, close=109, ema20=108, ema50=100, slope=2),
    ]
    return inst, bars, points, [confirmation_bar, future_confirmation_bar], confirmations


def test_vwap_pullback_generates_auditable_long_candidate_with_next_bar_trigger():
    inst, bars, points, bars15, points15 = vwap_fixture()
    registry = CandidateRegistry()
    results = evaluate_vwap_trend_pullback(
        inst, bars, points, bars15, points15, replay_id="replay-a", registry=registry
    )
    evaluation = results[-1]
    candidate = evaluation.candidate
    assert evaluation.status == SetupStatus.VALID and candidate is not None
    assert candidate.strategy_id == VWAP_PULLBACK_ID and candidate.direction == "LONG"
    assert candidate.entry_trigger_type == "stop_above_reclaim_candle_high"
    assert candidate.entry_trigger_price == D("103.05")  # TCS tick size is 0.05
    assert (
        candidate.stop_price
        < candidate.invalidation_price
        < candidate.entry_trigger_price
        < candidate.target_price
    )
    assert candidate.expected_reward_to_risk == D(2)
    assert candidate.estimated_net_return is None and candidate.cost_status == "unavailable"
    assert candidate.expiry_timestamp <= datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    assert (
        "confirmation_timestamp",
        bars15[0].timestamp.isoformat(),
    ) in candidate.indicator_snapshot
    assert candidate.replay_id == "replay-a" and len(candidate.configuration_hash) == 64
    snapshot = dict(candidate.indicator_snapshot)
    assert snapshot["reference_5m.open"] == "101.5"
    assert snapshot["confirmation_15m.close"] == "103"
    assert snapshot["strategy_config.target_reward_to_risk"] == "2.0"
    assert snapshot["5m.reclaim.indicator_config_hash"]
    assert snapshot["15m.confirmation.indicator_config_hash"]
    assert snapshot["quote_spread_bps"] is None
    assert candidate.data_source == "synthetic-fixture"


def test_vwap_pullback_rejects_failed_setup_invalid_data_and_missing_quote_when_required():
    inst, bars, points, bars15, points15 = vwap_fixture()
    poor_confirmation = [replace(points15[0], ema20=D(99), ema50=D(100))]
    assert (
        evaluate_vwap_trend_pullback(
            inst, bars, points, bars15[:1], poor_confirmation, replay_id="r"
        )[-1].candidate
        is None
    )
    invalid = list(points)
    invalid[-1] = replace(invalid[-1], data_valid=False)
    assert (
        evaluate_vwap_trend_pullback(inst, bars, invalid, bars15, points15, replay_id="r")[
            -1
        ].status
        == SetupStatus.INVALID_DATA
    )
    config = replace(VWAPPullbackConfig(), require_quotes=True)
    result = evaluate_vwap_trend_pullback(
        inst, bars, points, bars15, points15, replay_id="r", config=config
    )[-1]
    assert result.status == SetupStatus.REJECTED and result.reason == "quotes_required"


def test_vwap_pullback_is_independent_of_future_bars_and_htf_candles():
    inst, bars, points, bars15, points15 = vwap_fixture()
    baseline = evaluate_vwap_trend_pullback(inst, bars, points, bars15, points15, replay_id="r")[
        -1
    ].candidate
    future = candle(
        inst, bars[-1].timestamp + timedelta(minutes=5), open_=103, high=150, low=102, close=149
    )
    future_point = point_for(
        inst, future, close=149, vwap=101, atr=20, ema20=200, ema50=100, slope=30
    )
    extended = evaluate_vwap_trend_pullback(
        inst, bars + [future], points + [future_point], bars15, points15, replay_id="r"
    )
    assert baseline is not None and extended[-2].candidate is not None
    assert baseline.candidate_id == extended[-2].candidate.candidate_id
    assert (
        "confirmation_timestamp",
        bars15[0].timestamp.isoformat(),
    ) in baseline.indicator_snapshot


def orb_fixture(
    inst=None, *, session_open=None, breakout_close=105, breakout_high=106, breakout_volume=20
):
    inst = inst or equity()
    session_open = session_open or nse_open()
    bars = [
        candle(inst, session_open, open_=100, high=102, low=99, close=101),
        candle(inst, session_open + timedelta(minutes=5), open_=101, high=103, low=100, close=102),
        candle(
            inst, session_open + timedelta(minutes=10), open_=102, high=103, low=101, close=102.5
        ),
        candle(
            inst,
            session_open + timedelta(minutes=15),
            open_=103,
            high=breakout_high,
            low=102,
            close=breakout_close,
            volume=breakout_volume,
        ),
    ]
    points = [
        point_for(
            inst,
            bar,
            close=bar.close,
            vwap=101,
            atr=1,
            adx=30,
            ema20=104,
            ema50=102,
            slope=1,
            rvol=2,
        )
        for bar in bars
    ]
    return inst, bars, points


def test_orb_valid_nse_breakout_uses_complete_open_range_and_next_bar_trigger():
    inst, bars, points = orb_fixture()
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="orb-replay")[-1]
    candidate = result.candidate
    assert result.status == SetupStatus.VALID and candidate is not None
    assert candidate.strategy_id == ORB_ID
    assert ("opening_range_high", "103") in candidate.indicator_snapshot
    assert ("opening_range_low", "99") in candidate.indicator_snapshot
    assert candidate.entry_trigger_type == "stop_above_breakout_candle_high"
    assert candidate.entry_trigger_price == D("106.05")
    assert candidate.stop_price < candidate.invalidation_price < candidate.entry_trigger_price
    assert candidate.expiry_timestamp == bars[-1].end_timestamp + timedelta(minutes=30)
    assert candidate.expiry_timestamp < datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def test_orb_false_breakout_and_missing_opening_range_are_rejected():
    inst, bars, points = orb_fixture(breakout_close=103, breakout_high=106)
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1]
    assert result.status == SetupStatus.REJECTED and result.reason == "no_close_above_opening_range"
    inst, bars, points = orb_fixture()
    missing_bar = bars[1]
    bars = [bars[0], *bars[2:]]
    points = [point for point in points if point.timestamp != missing_bar.timestamp]
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1]
    assert (
        result.status == SetupStatus.INVALID_DATA
        and result.reason == "opening_range_incomplete_or_missing_bars"
    )


@pytest.mark.parametrize(
    ("session_date", "expected_open"),
    [
        (date(2026, 3, 9), datetime(2026, 3, 9, 13, 30, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 14, 30, tzinfo=UTC)),
    ],
)
def test_orb_uses_us_regular_session_open_across_dst(session_date, expected_open):
    inst = InstrumentMetadata(
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
    assert expected_open.date() == session_date
    inst, bars, points = orb_fixture(inst, session_open=expected_open)
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1]
    assert result.status == SetupStatus.VALID


def test_orb_allows_configured_range_regime_and_respects_quotes():
    inst, bars, points = orb_fixture()
    bars = [bars[0], bars[1], bars[2], bars[3]]
    points[-1] = replace(points[-1], adx14=D(10), ema20=D(99), ema50=D(101), ema20_slope=D(-1))
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1]
    assert result.status == SetupStatus.VALID
    assert ("regime", MarketRegime.RANGE_BOUND.value) in result.candidate.indicator_snapshot
    config = replace(OpeningRangeConfig(), require_quotes=True)
    inst, bars, points = orb_fixture()
    result = evaluate_opening_range_breakout(inst, bars, points, replay_id="r", config=config)[-1]
    assert result.reason == "quotes_required"
    quote = QuoteSnapshot(
        bars[-1].timestamp,
        bars[-1].end_timestamp + timedelta(minutes=1),
        D(105),
        D("105.01"),
        "quote",
    )
    result = evaluate_opening_range_breakout(
        inst, bars, points, replay_id="r", quotes={bars[-1].timestamp: quote}
    )[-1]
    assert result.reason == "quote_not_yet_available"


def test_orb_rejects_high_volatility_dislocation_and_low_relative_volume():
    inst, bars, points = orb_fixture()
    points[-1] = replace(points[-1], atr14=D("5"))
    assert (
        evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1].reason
        == "volatility_condition_failed"
    )
    inst, bars, points = orb_fixture(breakout_close=115, breakout_high=116)
    assert (
        "DISLOCATED"
        in evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1].reason
    )
    inst, bars, points = orb_fixture()
    points[-1] = replace(points[-1], relative_volume=D("1"))
    assert (
        evaluate_opening_range_breakout(inst, bars, points, replay_id="r")[-1].reason
        == "relative_volume_condition_failed"
    )


def test_orb_optional_retest_variant_waits_for_a_reclaim():
    inst = equity()
    start = nse_open()
    bars = [
        candle(inst, start, open_=100, high=102, low=99, close=101),
        candle(inst, start + timedelta(minutes=5), open_=101, high=103, low=100, close=102),
        candle(inst, start + timedelta(minutes=10), open_=102, high=103, low=101, close=102.5),
        candle(inst, start + timedelta(minutes=15), open_=103, high=106, low=102, close=105),
        candle(inst, start + timedelta(minutes=20), open_=104, high=105, low=102.5, close=104.5),
    ]
    points = [point_for(inst, bar, close=bar.close, atr=1, rvol=2) for bar in bars]
    result = evaluate_opening_range_breakout(
        inst,
        bars,
        points,
        replay_id="r",
        config=replace(OpeningRangeConfig(), require_retest=True),
    )[-1]
    assert result.status == SetupStatus.VALID
    assert result.candidate.reference_candle_timestamp == bars[-1].timestamp


def test_crypto_orb_disabled_by_default_and_requires_explicit_utc_anchor_to_enable():
    inst = crypto()
    bar = candle(
        inst, datetime(2026, 11, 2, 0, 0, tzinfo=UTC), open_=100, high=101, low=99, close=100
    )
    point = point_for(inst, bar)
    assert (
        evaluate_opening_range_breakout(inst, [bar], [point], replay_id="r")[0].reason
        == "crypto_orb_disabled_by_default"
    )
    with pytest.raises(ValueError, match="explicit UTC session anchor"):
        OpeningRangeConfig(allow_crypto=True)


def test_crypto_vwap_requires_and_records_explicit_utc_anchor():
    inst = crypto()
    bars = [
        candle(
            inst,
            datetime(2026, 11, 2, 0, 0, tzinfo=UTC) + timedelta(minutes=5 * i),
            open_=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
        )
        for i in range(3)
    ]
    points = [point_for(inst, bar, vwap=100, atr=2) for bar in bars]
    htf = candle(
        inst,
        datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        open_=100,
        high=102,
        low=99,
        close=101,
        interval=15,
    )
    htp = point_for(inst, htf, close=101, ema20=100, ema50=99, slope=1)
    config = replace(VWAPPullbackConfig(), crypto_session_anchor_utc=time(0, 0))
    # No pullback in this rising fixture, but the explicit anchor permits evaluation.
    result = evaluate_vwap_trend_pullback(
        inst, bars, points, [htf], [htp], replay_id="r", config=config
    )
    assert result[-1].reason != "crypto VWAP pullback requires an explicit UTC session anchor"
    with pytest.raises(ValueError, match="requires an explicit UTC session anchor"):
        evaluate_vwap_trend_pullback(inst, bars, points, [htf], [htp], replay_id="r")


def test_crypto_vwap_has_distinct_versioned_strategy_and_candle_semantics():
    inst, bars, points, bars15, points15 = vwap_fixture(crypto())
    config = CryptoVWAPPullbackConfig()
    assert config.utc_anchor == time(0, 0)
    assert config.price_input == "ohlcv_typical_price=(high+low+close)/3"
    results = evaluate_crypto_vwap_trend_pullback(
        inst, bars, points, bars15, points15, replay_id="synthetic-crypto-vwap"
    )
    candidate = next((item.candidate for item in results if item.candidate), None)
    assert candidate is not None
    assert candidate.strategy_id == CRYPTO_VWAP_PULLBACK_ID
    assert candidate.strategy_version == config.version
    assert dict(candidate.indicator_snapshot)["strategy_config.crypto_session_anchor_utc"] == "00:00:00"


def test_candidate_ids_dedupe_and_expiry_is_final():
    inst, bars, points, bars15, points15 = vwap_fixture()
    registry = CandidateRegistry()
    result = evaluate_vwap_trend_pullback(
        inst, bars, points, bars15, points15, replay_id="r", registry=registry
    )[-1]
    candidate = result.candidate
    assert candidate is not None
    duplicate = registry.emit(candidate, now=candidate.decision_timestamp)
    assert duplicate.status == SetupStatus.DUPLICATE
    expired = registry.advance(candidate.expiry_timestamp)
    assert len(expired) == 1 and expired[0].status == CandidateStatus.EXPIRED
    after_expiry = registry.emit(candidate, now=candidate.expiry_timestamp + timedelta(minutes=1))
    assert after_expiry.status == SetupStatus.EXPIRED
    assert after_expiry.candidate.status == CandidateStatus.EXPIRED


def test_range_mean_reversion_is_explicitly_disabled_and_emits_no_candidate():
    assert not RANGE_MEAN_REVERSION_ENABLED
    result = evaluate_range_mean_reversion(object())
    assert result[0].status == SetupStatus.REJECTED
    assert result[0].candidate is None
