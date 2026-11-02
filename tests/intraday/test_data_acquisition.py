"""Deterministic mocked coverage for read-only historical data providers."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tradingagents.intraday.data_sources.alpaca import AlpacaIEXClient
from tradingagents.intraday.data_sources.binance_public import (
    BinanceSpotArchiveClient,
    binance_spot_timestamp,
    verify_sha256,
)
from tradingagents.intraday.data_sources.common import ProviderError, RateLimiter
from tradingagents.intraday.data_sources.upstox import UpstoxHistoricalClient, month_chunks
from tradingagents.intraday.datasets import assess_quality, persist_dataset
from tradingagents.intraday.models import InstrumentMetadata, MarketBar


class Response:
    def __init__(self, payload=None, *, status=200, content=b"", text="", headers=None):
        self._payload, self.status_code = payload, status
        self.content, self.text, self.headers = content, text, headers or {}

    def json(self):
        return self._payload


class MockSession:
    def __init__(self, get):
        self._get = get
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._get(url, **kwargs)


def _instrument(calendar="XNSE"):
    return InstrumentMetadata(
        instrument_id="NSE_EQ|INE002A01018", symbol="RELIANCE", venue="NSE",
        asset_class="equity", base_currency="INR", quote_currency="INR",
        price_precision=2, tick_size=Decimal("0.05"), quantity_increment=Decimal("1"),
        exchange_timezone="Asia/Kolkata", session_calendar=calendar,
        data_source="upstox", provenance="mock instrument master",
    )


def _bar(stamp, close="100", volume="10"):
    return MarketBar(stamp, "NSE_EQ|INE002A01018", close, close, close, close, volume,
                     "mock", stamp.isoformat())


def test_month_windows_are_inclusive_and_calendar_bounded():
    assert month_chunks("2024-01-30", "2024-03-02") == [
        (date(2024, 1, 30), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    ]
    with pytest.raises(ValueError):
        month_chunks("2024-02-01", "2024-01-31")


def test_upstox_requires_environment_token_and_does_not_include_token_in_error(monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    client = UpstoxHistoricalClient(session=MockSession(lambda *_a, **_k: None))
    with pytest.raises(ProviderError, match="UPSTOX_ACCESS_TOKEN"):
        client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-02")


def test_upstox_instrument_lookup_uses_authoritative_master():
    content = gzip.compress(json.dumps([{
        "segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "RELIANCE",
        "instrument_key": "NSE_EQ|INE002A01018", "isin": "INE002A01018",
        "tick_size": 5.0, "lot_size": 1,
    }]).encode())
    session = MockSession(lambda *_a, **_k: Response(content=content))
    resolved = UpstoxHistoricalClient(session=session).resolve_nse_equity("RELIANCE")
    assert resolved.instrument_key == "NSE_EQ|INE002A01018"
    assert resolved.tick_size_raw == "5.0"
    assert resolved.metadata_sha256 == hashlib.sha256(content).hexdigest()


def test_upstox_instrument_lookup_rejects_ambiguous_match():
    content = gzip.compress(json.dumps([{
        "segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "TCS",
        "instrument_key": "NSE_EQ|1",
    }] * 2).encode())
    client = UpstoxHistoricalClient(session=MockSession(lambda *_a, **_k: Response(content=content)))
    with pytest.raises(ProviderError, match="2 NSE_EQ matches"):
        client.resolve_nse_equity("TCS")


def test_pilot_universe_is_configurable_but_limited_to_five():
    client = UpstoxHistoricalClient(session=MockSession(lambda *_a, **_k: None))
    with pytest.raises(ValueError, match="one to five"):
        client.resolve_nse_pilot(("A", "B", "C", "D", "E", "F"))


def test_upstox_parses_bars_and_chunks_by_month_without_printing_auth():
    payload = {"status": "success", "data": {"candles": [
        ["2024-01-02T03:45:00+00:00", 100, 102, 99, 101, 12, 0],
    ]}}
    session = MockSession(lambda *_a, **_k: Response(payload))
    pauses = []
    client = UpstoxHistoricalClient(
        access_token="secret-test-token", session=session,
        rate_limiter=RateLimiter(1000, clock=lambda: 0, sleep=pauses.append),
        sleep=lambda _seconds: None,
    )
    bars, _ = client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31")
    assert len(bars) == 1 and bars[0].close == Decimal("101")
    url, kwargs = session.calls[0]
    assert "%7C" in url
    assert kwargs["headers"]["Authorization"] == "Bearer secret-test-token"
    assert "secret-test-token" not in url


def test_upstox_retries_429_honoring_retry_after():
    responses = iter([Response(status=429, headers={"Retry-After": "3"}), Response({"status": "success", "data": {"candles": []}})])
    session = MockSession(lambda *_a, **_k: next(responses))
    pauses = []
    client = UpstoxHistoricalClient(access_token="x", session=session,
        rate_limiter=RateLimiter(1000, clock=lambda: 0, sleep=lambda _: None), sleep=pauses.append)
    bars, _ = client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31")
    assert bars == [] and pauses == [3.0] and len(session.calls) == 2


def test_upstox_month_cache_resumes_without_duplicate_request(tmp_path):
    payload = {"status": "success", "data": {"candles": [
        ["2024-01-02T03:45:00Z", 100, 101, 99, 100, 3],
    ]}}
    session = MockSession(lambda *_a, **_k: Response(payload))
    client = UpstoxHistoricalClient(access_token="x", session=session,
                                    rate_limiter=RateLimiter(1000))
    cache = tmp_path / "2024-01.json"
    first, _ = client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", raw_cache_path=cache)
    second, _ = client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", raw_cache_path=cache)
    assert first == second and len(session.calls) == 1


def test_upstox_refuses_corrupt_cached_month(tmp_path):
    cache = tmp_path / "month.json"
    cache.write_text("{partial", encoding="utf-8")
    client = UpstoxHistoricalClient(access_token="x", session=MockSession(lambda *_a, **_k: None))
    with pytest.raises(ProviderError, match="cached Upstox month"):
        client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", raw_cache_path=cache)


def test_upstox_rejects_duplicate_and_invalid_api_candles():
    payload = {"status": "success", "data": {"candles": [
        ["2024-01-02T03:45:00Z", 100, 101, 99, 100, 3],
        ["2024-01-02T03:45:00Z", 100, 101, 99, 100, 3],
    ]}}
    client = UpstoxHistoricalClient(access_token="x", session=MockSession(lambda *_a, **_k: Response(payload)),
                                    rate_limiter=RateLimiter(1000))
    with pytest.raises(ProviderError, match="duplicate"):
        client.fetch_month("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31")


def test_alpaca_paginates_and_forces_iex_feed():
    payloads = iter([
        {"bars": [{"t": "2024-01-02T14:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 5}], "next_page_token": "next"},
        {"bars": [{"t": "2024-01-02T14:31:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 6}], "next_page_token": None},
    ])
    session = MockSession(lambda *_a, **_k: Response(next(payloads)))
    client = AlpacaIEXClient(key="key", secret="secret", session=session,
                             rate_limiter=RateLimiter(1000), sleep=lambda _: None)
    bars = client.fetch_bars("AAPL", "2024-01-02", "2024-01-03")
    assert len(bars) == 2
    assert all(call[1]["params"]["feed"] == "iex" for call in session.calls)
    assert session.calls[1][1]["params"]["page_token"] == "next"


def test_alpaca_missing_credentials_is_reported():
    client = AlpacaIEXClient(session=MockSession(lambda *_a, **_k: None))
    with pytest.raises(ProviderError, match="APCA_API_KEY_ID"):
        client.fetch_bars("AAPL", "2024-01-01", "2024-01-02")


def test_binance_checksum_timestamp_units_and_terms_gate():
    content = b"archive"
    digest = hashlib.sha256(content).hexdigest()
    assert verify_sha256(content, f"{digest} file.zip") == digest
    assert binance_spot_timestamp(1_735_689_600_000, "2024-12").year == 2025
    assert binance_spot_timestamp(1_735_689_600_000_000, "2025-01").year == 2025
    with pytest.raises(ProviderError, match="terms must be acknowledged"):
        BinanceSpotArchiveClient().fetch_monthly_klines("BTCUSDT", "2025-01")
    with pytest.raises(ProviderError, match="mismatch"):
        verify_sha256(content, "0" * 64)


def test_binance_archive_verifies_and_parses_spot_klines():
    csv_content = b"1735689600000000,100,102,99,101,5,1735689659999999,500,2,3,300,0\n"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("BTCUSDT-1m-2025-01.csv", csv_content)
    archive_bytes = archive_buffer.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    session = MockSession(lambda url, **_k: Response(
        content=archive_bytes, text=digest if url.endswith("CHECKSUM") else ""))
    bars, metadata = BinanceSpotArchiveClient(session=session).fetch_monthly_klines(
        "BTCUSDT", "2025-01", terms_acknowledged=True)
    assert len(bars) == 1 and bars[0].close == Decimal("101")
    assert metadata["sha256"] == digest and bars[0].timestamp.year == 2025


def test_quality_finds_gaps_zero_volume_and_possible_corporate_action():
    # Three consecutive exchange minutes on a known XNSE session.
    times = [datetime(2024, 1, 2, 3, minute, tzinfo=UTC) for minute in (45, 46, 48)]
    bars = [_bar(times[0], "100"), _bar(times[1], "100", "0"), _bar(times[2], "150")]
    report = assess_quality(bars, _instrument(), expected_start=date(2024, 1, 2), expected_end=date(2024, 1, 2))
    assert report.records == 3
    assert report.zero_volume_intervals == 1 and not report.zero_volume_is_confirmed_no_trade
    assert report.missing_intervals >= 1
    assert report.abnormal_price_jumps[0]["flag"] == "possible_corporate_action_or_dislocation"


def test_manifest_and_parquet_artifacts_are_reproducible_and_immutable(tmp_path):
    times = [datetime(2024, 1, 2, 3, minute, tzinfo=UTC) for minute in (45, 46, 47, 48, 49)]
    bars = [_bar(ts, str(100 + idx)) for idx, ts in enumerate(times)]
    raw = json.dumps({"candles": [b.source_timestamp for b in bars]}).encode()
    result = persist_dataset(
        bars, _instrument(), provider="mock", source_reference="mock://candles",
        requested_start=date(2024, 1, 2), requested_end=date(2024, 1, 2),
        raw_payloads=[("chunk.json", raw)], instrument_metadata_version="mock:v1",
        source_terms_reference="mock terms", data_root=tmp_path,
    )
    assert result["record_count"] == 5
    assert {key.split(":")[0] for key in result["artifacts"]} == {"1m", "5m"}
    assert "dataset_id" in result and result["raw_payloads"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    changed = persist_dataset(
            bars, _instrument(), provider="mock", source_reference="mock://candles",
            requested_start=date(2024, 1, 2), requested_end=date(2024, 1, 2),
            raw_payloads=[("chunk.json", b"different")], instrument_metadata_version="mock:v1",
            source_terms_reference="mock terms", data_root=tmp_path,
        )
    assert changed["dataset_id"] != result["dataset_id"]


def test_repeated_dataset_persistence_returns_original_manifest(tmp_path):
    times = [datetime(2024, 1, 2, 3, minute, tzinfo=UTC) for minute in (45, 46, 47, 48, 49)]
    bars = [_bar(ts, "100") for ts in times]
    kwargs = {
        "provider": "mock", "source_reference": "mock://candles",
        "requested_start": date(2024, 1, 2), "requested_end": date(2024, 1, 2),
        "raw_payloads": [("chunk.json", b"same")], "instrument_metadata_version": "mock:v1",
        "source_terms_reference": "mock terms", "data_root": tmp_path,
    }
    first = persist_dataset(bars, _instrument(), **kwargs)
    second = persist_dataset(bars, _instrument(), **kwargs)
    assert first == second

