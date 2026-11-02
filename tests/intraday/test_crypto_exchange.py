"""Current exchange-rule fixture and independent Nautilus-fill projection tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tradingagents.intraday.crypto_exchange import (
    ExchangeFilterError,
    canonicalize_binance_spot_data,
    parse_binance_spot_exchange_info,
)
from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountLedger,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.models import InstrumentMetadata

ROOT = Path(__file__).resolve().parents[2]
RAW_FILTERS = ROOT / "tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-raw.json"
RETRIEVED_AT = datetime.fromisoformat("2026-11-02T17:07:33.792021+00:00")


def test_retained_binance_filter_snapshot_is_current_spot_and_fully_parsed():
    raw = RAW_FILTERS.read_bytes()
    filters = parse_binance_spot_exchange_info(raw, retrieved_at=RETRIEVED_AT)
    assert filters.symbol == "BTCUSDT" and filters.spot_enabled
    assert filters.tick_size == D("0.01000000")
    assert filters.lot_step_size == D("0.00001000")
    assert filters.lot_min_quantity == D("0.00001000")
    assert filters.min_notional == D("5.00000000")
    assert filters.market_max_quantity == D("122.91494833")
    assert filters.raw_response_sha256 == "f47dd9cae7c55b04273c305e3d66778a3c4afe47d0448ed4826544036bda248c"
    assert filters.exchange_server_time_utc is not None


def test_filter_parser_fails_closed_when_required_rule_is_missing():
    payload = json.loads(RAW_FILTERS.read_text())
    payload["symbols"][0]["filters"] = [
        item for item in payload["symbols"][0]["filters"] if item["filterType"] != "LOT_SIZE"
    ]
    with pytest.raises(ExchangeFilterError, match="LOT_SIZE filter is missing"):
        parse_binance_spot_exchange_info(
            json.dumps(payload).encode(), retrieved_at=RETRIEVED_AT
        )


def test_fee_configuration_requires_maker_taker_and_timestamped_provenance():
    config = CryptoSpotFeeConfiguration(
        D("0.0008"), D("0.001"), "USDT", "synthetic research assumption",
        RETRIEVED_AT, False, "synthetic-fees-v1",
    )
    assert config.cost_status == "COST_INCOMPLETE"
    assert config.taker_model().fee_rate == D("0.001")
    with pytest.raises(ValueError, match="timestamp"):
        CryptoSpotFeeConfiguration(D(0), D(0), "USDT", "test", datetime(2025, 1, 1), False, "v1")


def test_nautilus_fill_projection_does_not_reserve_or_double_debit_balances():
    from tests.intraday.test_crypto_execution import account, spec

    ledger = CryptoSpotAccountLedger(spec(), account())
    ledger.project_nautilus_fill(
        "nautilus-buy-1", "BUY", D("0.2"), D("1000"), D("0.2000"), "USDT",
        timestamp=RETRIEVED_AT,
    )
    assert ledger.balances == {"USDT": D("799.8000"), "BTC": D("0.2")}
    assert ledger.reserved_balances == {"USDT": D(0), "BTC": D(0)}
    assert ledger.project_nautilus_fill(
        "nautilus-sell-1", "SELL", D("0.1"), D("1100"), D("0.1100"), "USDT",
        timestamp=RETRIEVED_AT + timedelta(seconds=1),
    ).realized_pnl_quote == D("9.7900")
    assert ledger.reserved_balances["USDT"] == ledger.reserved_balances["BTC"] == 0
    with pytest.raises(ValueError, match="overdraw"):
        ledger.project_nautilus_fill(
            "nautilus-sell-2", "SELL", D("1"), D("1100"), D("1.1"), "USDT",
            timestamp=RETRIEVED_AT + timedelta(seconds=2),
        )


def test_binance_filters_must_match_execution_instrument():
    from tradingagents.intraday.crypto_execution import CryptoRiskError, CryptoSpotExecutionSpec

    filters = parse_binance_spot_exchange_info(RAW_FILTERS.read_bytes(), retrieved_at=RETRIEVED_AT)
    instrument = InstrumentMetadata(
        "BTCUSDT.BINANCE_SPOT", "BTCUSDT", "BINANCE_SPOT", "crypto_spot", "BTC", "USDT",
        2, D("0.01"), D("0.00001"), "UTC", "24/7", "binance-public-data", "current filter fixture",
        D("0.00001"), D("5"),
    )
    assert CryptoSpotExecutionSpec(
        instrument, "USDT", 5, "USDT", "fee-v1", "Binance exchangeInfo", RETRIEVED_AT, filters
    ).exchange_filters == filters
    with pytest.raises(CryptoRiskError, match="conflicts"):
        CryptoSpotExecutionSpec(
            instrument, "USDT", 5, "USDT", "fee-v1", "Binance exchangeInfo", RETRIEVED_AT,
            replace(filters, tick_size=D("0.1")),
        )


def test_phase4b_manifest_identity_maps_explicitly_to_nautilus_identifier():
    from tradingagents.intraday.models import MarketBar

    source = InstrumentMetadata(
        "BINANCE_SPOT:BTCUSDT", "BTCUSDT", "BINANCE_SPOT", "crypto_spot", "BTC", "USDT",
        2, D("0.01"), D("0.00001"), "UTC", "24/7", "binance-public-data",
        "archive-2025-01", D("0.00001"), D("5"),
    )
    bar = MarketBar(
        datetime(2025, 1, 1, tzinfo=UTC), source.instrument_id,
        D("100"), D("101"), D("99"), D("100"), D("1"), "source-1", "fixture", 1,
    )
    mapped_instrument, mapped_bars = canonicalize_binance_spot_data(source, [bar])
    assert mapped_instrument.instrument_id == "BTCUSDT.BINANCE_SPOT"
    assert mapped_bars[0].instrument_id == mapped_instrument.instrument_id
    assert "BINANCE_SPOT:BTCUSDT -> BTCUSDT.BINANCE_SPOT" in mapped_instrument.provenance
