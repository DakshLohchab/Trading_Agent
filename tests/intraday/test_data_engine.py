from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingagents.intraday.aggregation import aggregate_bars
from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.ingest import read_market_data
from tradingagents.intraday.models import CurrencyConversionMetadata, InstrumentMetadata, MarketBar
from tradingagents.intraday.replay import HistoricalReplay, ReplayMetadataStore

UTC = UTC
ROOT = Path(__file__).resolve().parents[2]


def instrument(
    calendar="NSE",
    *,
    instrument_id="TCS.NSE",
    venue="NSE",
    asset="equity",
    timezone_name="Asia/Kolkata",
):
    return InstrumentMetadata(
        instrument_id=instrument_id,
        symbol=instrument_id.split(".")[0],
        venue=venue,
        asset_class=asset,
        base_currency="INR" if calendar in ("NSE", "BSE") else "USD",
        quote_currency="INR" if calendar in ("NSE", "BSE") else "USD",
        price_precision=2,
        tick_size=Decimal("0.05"),
        quantity_increment=Decimal("1"),
        exchange_timezone=timezone_name,
        session_calendar=calendar,
        data_source="synthetic-fixture",
        provenance="tests/intraday/test_data_engine.py",
    )


def bar(stamp, *, inst=None, price=100, volume=10, source="fixture", quality="observed"):
    inst = inst or instrument()
    value = Decimal(str(price))
    high, low = (value, value) if quality == "confirmed_no_trade" else (value + 1, value - 1)
    return MarketBar(
        stamp,
        inst.instrument_id,
        value,
        high,
        low,
        value,
        Decimal(str(volume)),
        source,
        stamp.isoformat(),
        quality=quality,
    )


def nse_open():
    return datetime(2026, 11, 2, 3, 45, tzinfo=UTC)


def test_instrument_identity_is_venue_qualified_and_validated():
    item = instrument()
    assert item.instrument_id == "TCS.NSE"
    with pytest.raises(ValueError, match="ticker alone"):
        instrument(instrument_id="TCS")
    fx = CurrencyConversionMetadata("INR", "USD", Decimal("0.012"), nse_open(), "fixture-fx")
    assert fx.observed_at == nse_open()
    with pytest.raises(ValueError, match="timezone-aware"):
        CurrencyConversionMetadata(
            "INR", "USD", Decimal("0.012"), datetime(2026, 1, 1), "fixture-fx"
        )
    with pytest.raises(ValueError, match="positive"):
        InstrumentMetadata(
            "X.NSE", "X", "NSE", "equity", "INR", "INR", 2, 0, 1, "Asia/Kolkata", "NSE", "x", "x"
        )


def test_timestamp_normalization_preserves_source_local_text(tmp_path):
    path = tmp_path / "bars.csv"
    write_rows(
        path,
        [
            {
                "timestamp": "2026-11-02T09:15:00",
                "instrument_id": "TCS.NSE",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 2,
                "source_id": "csv",
                "source_timestamp": "source-row-1",
                "base_interval_minutes": 1,
            }
        ],
    )
    data = read_market_data(path, instrument())
    assert data.bars[0].timestamp == nse_open()
    assert data.bars[0].exchange_local_timestamp == "2026-11-02T09:15:00"
    assert len(data.file_sha256) == 64


def test_dst_aware_us_session_changes_utc_open():
    cal = SessionCalendar("NYSE")
    march = cal.sessions(date(2026, 3, 9), date(2026, 3, 9))[0]
    november = cal.sessions(date(2026, 11, 2), date(2026, 11, 2))[0]
    assert march.open_utc.hour == 13
    assert november.open_utc.hour == 14


def test_ambiguous_dst_wall_time_requires_an_explicit_offset(tmp_path):
    inst = instrument(
        "NYSE", instrument_id="ABC.XNYS", venue="NYSE", timezone_name="America/New_York"
    )
    path = tmp_path / "dst.csv"
    write_rows(path, [row_for(datetime(2026, 11, 1, 1, 30), inst)])
    with pytest.raises(ValueError, match="ambiguous or nonexistent"):
        read_market_data(path, inst)


def test_nse_session_boundaries_and_holiday():
    cal = SessionCalendar("NSE")
    session = cal.sessions(date(2026, 11, 2), date(2026, 11, 2))[0]
    assert session.open_utc == nse_open()
    assert session.close_utc == datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    assert cal.sessions(date(2026, 10, 2), date(2026, 10, 2)) == []
    assert cal.session_for_bar(session.close_utc) is None


def test_bse_session_schedule_is_supported():
    session = SessionCalendar("BSE").sessions(date(2026, 11, 2), date(2026, 11, 2))[0]
    assert session.open_utc == nse_open()
    assert session.close_utc == datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def test_us_session_holiday_and_early_close():
    cal = SessionCalendar("NYSE")
    assert cal.sessions(date(2026, 11, 26), date(2026, 11, 26)) == []
    early = cal.sessions(date(2026, 11, 27), date(2026, 11, 27))[0]
    assert early.close_utc == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_nasdaq_calendar_configuration_exists():
    cal = SessionCalendar("NASDAQ")
    assert cal.sessions(date(2026, 11, 2), date(2026, 11, 2))[0].open_utc.hour == 14


def test_crypto_24_7_calendar_spans_weekends():
    cal = SessionCalendar("24/7")
    sessions = cal.sessions(date(2026, 11, 7), date(2026, 11, 8))
    assert len(sessions) == 2
    assert cal.session_for_bar(datetime(2026, 11, 8, 23, 59, tzinfo=UTC)) is not None


def test_five_minute_aggregation_math_and_boundaries():
    inst = instrument()
    source = [
        bar(nse_open() + timedelta(minutes=i), inst=inst, price=100 + i, volume=i + 1)
        for i in range(5)
    ]
    result = aggregate_bars(source, inst, 5)
    assert len(result) == 1
    candle = result[0]
    assert candle.is_valid
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (
        Decimal(100),
        Decimal(105),
        Decimal(99),
        Decimal(104),
        Decimal(15),
    )
    assert candle.end_timestamp == nse_open() + timedelta(minutes=5)


def test_fifteen_minute_aggregation():
    inst = instrument()
    result = aggregate_bars(
        [bar(nse_open() + timedelta(minutes=i), inst=inst, price=100 + i) for i in range(15)],
        inst,
        15,
    )
    assert len(result) == 1 and result[0].is_valid
    assert result[0].close == 114 and result[0].volume == 150


def test_missing_minute_marks_aggregate_invalid_without_filling():
    inst = instrument()
    source = [bar(nse_open() + timedelta(minutes=i), inst=inst) for i in (0, 1, 3, 4)]
    result = aggregate_bars(source, inst, 5)
    assert len(result) == 1
    assert not result[0].is_valid and result[0].invalid_reason == "missing_source_intervals"
    assert result[0].volume == 40


def test_fully_missing_aggregate_bucket_between_observed_data_is_reported():
    inst = instrument()
    source = [bar(nse_open() + timedelta(minutes=i), inst=inst) for i in (0, 10)]
    result = aggregate_bars(source, inst, 5)
    assert len(result) == 2
    assert not result[0].is_valid and not result[1].is_valid
    assert result[1].open is None


def test_explicit_zero_trade_minute_is_not_missing():
    inst = instrument()
    source = [
        bar(nse_open() + timedelta(minutes=i), inst=inst, volume=0, quality="confirmed_no_trade")
        for i in range(5)
    ]
    result = aggregate_bars(source, inst, 5)
    assert result[0].is_valid and result[0].volume == 0


def test_duplicate_and_out_of_order_rows_rejected(tmp_path):
    inst = instrument()
    rows = [row_for(nse_open() + timedelta(minutes=i), inst) for i in (0, 1, 1)]
    path = tmp_path / "duplicate.csv"
    write_rows(path, rows)
    with pytest.raises(ValueError, match="duplicate"):
        read_market_data(path, inst)
    rows = [row_for(nse_open() + timedelta(minutes=i), inst) for i in (1, 0)]
    write_rows(path, rows)
    with pytest.raises(ValueError, match="out-of-order"):
        read_market_data(path, inst)


def test_ingestion_reports_missing_scheduled_intervals(tmp_path):
    inst = instrument()
    path = tmp_path / "gap.csv"
    write_rows(path, [row_for(nse_open() + timedelta(minutes=i), inst) for i in (0, 2)])
    result = read_market_data(path, inst)
    assert [(gap.start, gap.end) for gap in result.gaps] == [
        (nse_open() + timedelta(minutes=1), nse_open() + timedelta(minutes=2))
    ]


def test_invalid_ohlcv_and_negative_prices_rejected(tmp_path):
    inst = instrument()
    rows = [row_for(nse_open(), inst, high=8, low=10)]
    path = tmp_path / "bad.csv"
    write_rows(path, rows)
    with pytest.raises(ValueError, match="high must"):
        read_market_data(path, inst)
    rows = [row_for(nse_open(), inst, open=-1, high=1, low=-2, close=0)]
    write_rows(path, rows)
    with pytest.raises(ValueError, match="negative prices"):
        read_market_data(path, inst)


def test_session_crossing_and_incomplete_buckets_are_not_emitted_as_valid():
    inst = instrument(
        "NYSE", instrument_id="ABC.XNYS", venue="NYSE", timezone_name="America/New_York"
    )
    early_close_open = datetime(2026, 11, 27, 17, 58, tzinfo=UTC)
    source = [bar(early_close_open + timedelta(minutes=i), inst=inst) for i in range(2)]
    candles = aggregate_bars(source, inst, 5)
    assert len(candles) == 1 and not candles[0].is_valid
    assert candles[0].end_timestamp == early_close_open + timedelta(minutes=2)
    with pytest.raises(ValueError, match="outside NYSE session"):
        aggregate_bars([bar(datetime(2026, 11, 27, 18, 0, tzinfo=UTC), inst=inst)], inst, 5)


def test_replay_filtering_determinism_checksum_and_sqlite_metadata(tmp_path):
    inst = instrument()
    source = [bar(nse_open() + timedelta(minutes=i), inst=inst, source=f"s{i}") for i in (0, 1, 2)]
    store = ReplayMetadataStore(tmp_path / "replays.sqlite")
    kwargs = {
        "instruments": {inst.instrument_id: inst},
        "bars": source,
        "data_checksums": {"bars.csv": "abc123"},
        "start": nse_open() + timedelta(minutes=1),
        "end": nse_open() + timedelta(minutes=3),
        "metadata_store": store,
    }
    first = HistoricalReplay(**kwargs)
    second = HistoricalReplay(**kwargs)
    assert first.metadata.replay_id == second.metadata.replay_id
    assert [(e.sequence, e.available_at, e.bar.source_id) for e in first.iter_events()] == [
        (e.sequence, e.available_at, e.bar.source_id) for e in second.iter_events()
    ]
    assert [e.bar.source_id for e in first.events] == ["s0", "s1", "s2"]
    saved = store.get(first.metadata.replay_id)
    assert saved["data_checksums"] == [["bars.csv", "abc123"]]
    assert saved["calendar_provenance"] and saved["candle_interval_minutes"] == 1


def test_replay_rejects_duplicate_events_and_naive_boundaries():
    inst = instrument()
    repeated = bar(nse_open(), inst=inst)
    with pytest.raises(ValueError, match="duplicate replay event"):
        HistoricalReplay({inst.instrument_id: inst}, [repeated, repeated])
    with pytest.raises(ValueError, match="timezone-aware"):
        HistoricalReplay({inst.instrument_id: inst}, [], start=datetime(2026, 1, 1))


def test_parquet_reader_roundtrip_when_engine_is_available(tmp_path):
    pytest.importorskip("pyarrow")
    import pandas as pd

    inst = instrument()
    path = tmp_path / "bars.parquet"
    pd.DataFrame([row_for(nse_open(), inst)]).to_parquet(path, index=False)
    assert len(read_market_data(path, inst).bars) == 1


def test_nautilus_v1231_delivery_and_repeatability():
    executable = ROOT / ".venv-nautilus" / "bin" / "python"
    if not executable.exists():
        pytest.skip("isolated .venv-nautilus is unavailable")
    code = (
        r'''
import json
import sys, types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
package = types.ModuleType("tradingagents")
package.__path__ = ["'''
        + str(ROOT / "tradingagents")
        + r""""]
sys.modules["tradingagents"] = package
from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata
from tradingagents.intraday.nautilus_v1 import run_bar_simulation
from tradingagents.intraday.crypto_execution import CryptoSpotAccountConfig
inst = InstrumentMetadata("NVDA.XNAS", "NVDA", "NASDAQ", "equity", "USD", "USD", 2, Decimal("0.01"), Decimal("1"), "America/New_York", "NASDAQ", "fixture", "test")
start = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
bars = [AggregatedCandle(start + timedelta(minutes=5*i), start + timedelta(minutes=5*(i+1)), inst.instrument_id, 5, Decimal(10+i), Decimal(11+i), Decimal(9+i), Decimal(10+i), Decimal(100), True) for i in range(2)]
a = run_bar_simulation(inst, bars)
b = run_bar_simulation(inst, bars)
crypto = InstrumentMetadata("BTCUSDT.BINANCE", "BTCUSDT", "BINANCE", "crypto_spot", "BTC", "USDT", 2, Decimal("0.01"), Decimal("0.001"), "UTC", "24/7", "fixture", "test")
crypto_bar = AggregatedCandle(start, start + timedelta(minutes=5), crypto.instrument_id, 5, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("0.125"), True)
account = CryptoSpotAccountConfig("crypto-test", "USDT", Decimal("1000"), Decimal("0"), None, Decimal("10"), Decimal("30"), 1, 3, 1440, "UTC", 5)
c = run_bar_simulation(crypto, [crypto_bar], crypto_account=account)
print(json.dumps({"a": a["delivered_bars"], "b": b["delivered_bars"], "count": a["bar_count"], "type": a["bar_type"], "crypto_count": c["bar_count"], "crypto_account_type": c["account_type"], "crypto_account_currency": c["account_currency"]}))
"""
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [str(executable), "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["a"] == output["b"]
    assert output["count"] == 2
    assert output["crypto_count"] == 1
    assert output["crypto_account_type"] == "CASH"
    assert output["crypto_account_currency"] == "USDT"
    assert "5-MINUTE-LAST" in output["type"]


def row_for(stamp, inst, **overrides):
    data = {
        "timestamp": stamp.isoformat(),
        "instrument_id": inst.instrument_id,
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "volume": 2,
        "source_id": "csv-source",
        "source_timestamp": "provider-time",
        "base_interval_minutes": 1,
    }
    data.update(overrides)
    return data


def write_rows(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
