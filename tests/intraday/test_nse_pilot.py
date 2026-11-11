from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from tradingagents.intraday import nse_pilot
from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.data_sources.upstox import ResolvedInstrument
from tradingagents.intraday.models import InstrumentMetadata, MarketBar
from tradingagents.intraday.strategies import VWAPPullbackConfig


def _instrument() -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id="NSE_EQ|INE002A01018", symbol="RELIANCE", venue="NSE",
        asset_class="equity", base_currency="INR", quote_currency="INR",
        price_precision=2, tick_size=Decimal("0.05"), quantity_increment=Decimal("1"),
        exchange_timezone="Asia/Kolkata", session_calendar="XNSE", data_source="upstox",
        provenance="test fixture",
    )


def _bar(stamp: datetime) -> MarketBar:
    return MarketBar(
        stamp, "NSE_EQ|INE002A01018", "100", "100.05", "99.95", "100", "0",
        "test", stamp.isoformat(), exchange_local_timestamp=stamp.isoformat(),
    )


def test_latest_completed_session_does_not_select_open_session():
    calendar = SessionCalendar("XNSE")
    before_close = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    latest = nse_pilot.latest_completed_session(calendar, before_close)
    assert latest < date(2026, 11, 10)


def test_explicit_session_count_uses_exact_trading_sessions():
    calendar = SessionCalendar("XNSE")
    selected = nse_pilot.trailing_sessions(calendar, date(2026, 11, 9), 60)
    assert len(selected) == 60
    assert selected[-1].session_date == date(2026, 11, 9)


def test_tick_size_unit_ambiguity_fails_closed():
    tick, blocker = nse_pilot.resolve_tick_size("5.0", 2)
    assert tick is None
    assert "does not define its unit" in blocker


def test_tick_size_unit_remains_blocked_even_when_precision_is_valid():
    tick, blocker = nse_pilot.resolve_tick_size("0.05", 2)
    assert tick is None
    assert "does not define its unit" in blocker


def test_session_completeness_reports_missing_and_zero_volume_without_repair():
    session = SessionCalendar("XNSE").sessions(date(2024, 1, 2), date(2024, 1, 2))[0]
    bars = [_bar(stamp) for stamp in SessionCalendar("XNSE").expected_bar_starts(session, 1)[:-1]]
    quality = nse_pilot.validate_session_data(bars, _instrument(), [session])
    assert quality["complete_session_count"] == 0
    assert quality["incomplete_last_session"] is True
    assert quality["zero_volume_intervals"] == len(bars)
    assert quality["missing_expected_intervals"] == 1
    assert quality["quality_passed"] is False
    assert len(bars) == 374


def test_missing_token_emits_blocked_report_without_provider_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)

    class NeverCalled:
        def resolve_nse_equity(self, *_args, **_kwargs):
            raise AssertionError("instrument master must not be requested without auth")

    report = nse_pilot.run_pilot(
        session_count=1, end_date=date(2026, 11, 9), output_dir=tmp_path,
        client=NeverCalled(), now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )
    assert report["status"] == "BLOCKED_AUTH"
    assert report["broker_orders_called"] is False
    assert "UPSTOX_ACCESS_TOKEN" in report["blockers"][0]
    assert len(list(tmp_path.glob("nse-pilot-*.json"))) == 1
    assert len(list(tmp_path.glob("nse-pilot-*.md"))) == 1


def test_equity_signal_path_uses_existing_equity_config(monkeypatch):
    session = SessionCalendar("XNSE").sessions(date(2024, 1, 2), date(2024, 1, 2))[0]
    bars = [_bar(stamp) for stamp in SessionCalendar("XNSE").expected_bar_starts(session, 1)]
    observed = {}

    def capture(instrument, candles_5m, indicators_5m, candles_15m, indicators_15m,
                *, replay_id, config):
        observed.update(instrument=instrument, replay_id=replay_id, config=config,
                        five_count=len(candles_5m), fifteen_count=len(candles_15m))
        return ()

    monkeypatch.setattr(nse_pilot, "evaluate_vwap_trend_pullback", capture)
    assert nse_pilot.evaluate_equity_signals(bars, _instrument(), "dataset-1") == ()
    assert observed["instrument"].asset_class == "equity"
    assert isinstance(observed["config"], VWAPPullbackConfig)
    assert observed["config"].crypto_session_anchor_utc is None
    assert observed["replay_id"] == "nse-pilot:dataset-1"
    assert observed["five_count"] == 75
    assert observed["fifteen_count"] == 25


def test_pilot_has_no_broker_order_api_imports_or_calls():
    source_path = Path(nse_pilot.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    source = source_path.read_text(encoding="utf-8").lower()
    assert not any("nautilus" in module or "execution" in module for module in imported)
    assert "place_order" not in source
    assert "cancel_order" not in source
    assert "modify_order" not in source


def test_resolved_instrument_fixture_carries_raw_tick_and_provenance():
    resolved = ResolvedInstrument(
        trading_symbol="RELIANCE", instrument_key="NSE_EQ|INE002A01018",
        isin="INE002A01018", tick_size_raw="5.0", lot_size="1", instrument_type="EQ",
        metadata_sha256="a" * 64, metadata_retrieved_at="2026-11-10T00:00:00+00:00",
        metadata_version="sha256:" + "a" * 64,
    )
    assert resolved.instrument_key.startswith("NSE_EQ|")
    assert resolved.tick_size_raw == "5.0"
