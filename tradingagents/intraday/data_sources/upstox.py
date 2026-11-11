"""Read-only Upstox Historical Candle V3 and daily instrument-master client."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from tradingagents.intraday.data_sources.common import ProviderError, RateLimiter, retry_call
from tradingagents.intraday.models import MarketBar

API_ROOT = "https://api.upstox.com/v3"
NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SOURCE_TERMS = "https://upstox.com/developer/api-documentation/"
DEFAULT_NSE_PILOT_SYMBOLS = ("RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK")


@dataclass(frozen=True, slots=True)
class ResolvedInstrument:
    trading_symbol: str
    instrument_key: str
    isin: str | None
    tick_size_raw: str | None
    lot_size: str | None
    instrument_type: str
    metadata_sha256: str
    metadata_retrieved_at: str
    metadata_version: str


class _HttpFailure(Exception):
    def __init__(self, status: int, retry_after: str | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


def _utc_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime arguments must be timezone-aware")
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def month_chunks(start: date | str, end: date | str) -> list[tuple[date, date]]:
    """Split inclusive dates into calendar-month windows for 1–15m candles."""
    first, last = _utc_date(start), _utc_date(end)
    if last < first:
        raise ValueError("end date must not precede start date")
    chunks = []
    cursor = first
    while cursor <= last:
        month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        chunk_end = min(last, month_end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


class UpstoxHistoricalClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        session: requests.Session | Any | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep=time.sleep,
        attempts: int = 5,
    ):
        self._provided_token = access_token
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter(10.0)
        self.sleep = sleep
        self.attempts = attempts

    def _token(self) -> str:
        token = self._provided_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not token:
            raise ProviderError("UPSTOX_ACCESS_TOKEN is required for historical downloads")
        return token

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        return isinstance(exc, requests.RequestException) or (
            isinstance(exc, _HttpFailure) and (exc.status == 429 or exc.status >= 500)
        )

    @staticmethod
    def _retry_after(exc: Exception) -> str | None:
        return exc.retry_after if isinstance(exc, _HttpFailure) else None

    def _get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            self.rate_limiter.wait()
            try:
                response = self.session.get(url, headers=headers, timeout=30)
            except requests.RequestException:
                raise
            if response.status_code >= 400:
                raise _HttpFailure(response.status_code, response.headers.get("Retry-After"))
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ProviderError("Upstox returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ProviderError("Upstox returned an invalid response shape")
            return payload

        try:
            return retry_call(
                request,
                attempts=self.attempts,
                sleep=self.sleep,
                retryable=self._retryable,
                retry_after=self._retry_after,
            )
        except _HttpFailure as exc:
            raise ProviderError(f"Upstox request failed with HTTP {exc.status}") from None
        except requests.RequestException as exc:
            raise ProviderError(f"Upstox transport failure: {type(exc).__name__}") from None

    def resolve_nse_equity(
        self,
        symbol: str,
        *,
        cache_path: str | Path | None = None,
    ) -> ResolvedInstrument:
        """Resolve exactly one current NSE cash equity from authoritative daily metadata."""
        if not symbol.strip():
            raise ValueError("symbol cannot be blank")
        retrieved_at = datetime.now(UTC).isoformat()
        if cache_path and Path(cache_path).exists():
            path = Path(cache_path)
            compressed = path.read_bytes()
            timestamp_path = path.with_name(path.name + ".metadata.json")
            try:
                cached_metadata = json.loads(timestamp_path.read_text(encoding="utf-8"))
                if cached_metadata.get("sha256") == hashlib.sha256(compressed).hexdigest():
                    retrieved_at = cached_metadata["retrieved_at_utc"]
                else:
                    retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        else:
            self.rate_limiter.wait()
            response = self.session.get(NSE_MASTER_URL, timeout=60)
            if response.status_code >= 400:
                raise ProviderError(f"Upstox instrument master failed with HTTP {response.status_code}")
            compressed = response.content
            if cache_path:
                path = Path(cache_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(path, compressed)
                _atomic_write(
                    path.with_name(path.name + ".metadata.json"),
                    (json.dumps({"retrieved_at_utc": retrieved_at,
                                 "sha256": hashlib.sha256(compressed).hexdigest()},
                                sort_keys=True) + "\n").encode(),
                )
        checksum = hashlib.sha256(compressed).hexdigest()
        try:
            content = gzip.decompress(compressed) if compressed[:2] == b"\x1f\x8b" else compressed
            rows = json.loads(content)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("Upstox instrument master is not valid JSON/GZIP") from exc
        if not isinstance(rows, list):
            raise ProviderError("Upstox instrument master has an invalid shape")
        matches = [
            row for row in rows
            if isinstance(row, dict)
            and row.get("segment") == "NSE_EQ"
            and row.get("instrument_type") == "EQ"
            and row.get("trading_symbol", "").upper() == symbol.upper()
        ]
        if len(matches) != 1:
            raise ProviderError(f"instrument master resolved {len(matches)} NSE_EQ matches for {symbol}")
        row = matches[0]
        key = row.get("instrument_key")
        if not isinstance(key, str) or not key.startswith("NSE_EQ|"):
            raise ProviderError("instrument master entry has no valid NSE_EQ instrument_key")
        isin = row.get("isin")
        if not isinstance(isin, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", isin):
            raise ProviderError("instrument master entry has no valid ISIN")
        return ResolvedInstrument(
            trading_symbol=row["trading_symbol"],
            instrument_key=key,
            isin=isin,
            tick_size_raw=str(row["tick_size"]) if row.get("tick_size") is not None else None,
            lot_size=str(row["lot_size"]) if row.get("lot_size") is not None else None,
            instrument_type=row["instrument_type"],
            metadata_sha256=checksum,
            metadata_retrieved_at=retrieved_at,
            metadata_version=f"sha256:{checksum}",
        )

    def resolve_nse_pilot(
        self,
        symbols: tuple[str, ...] = DEFAULT_NSE_PILOT_SYMBOLS,
        *,
        cache_path: str | Path | None = None,
    ) -> tuple[ResolvedInstrument, ...]:
        """Resolve a configurable universe of at most five current NSE equities."""
        if not 1 <= len(symbols) <= 5 or len({symbol.upper() for symbol in symbols}) != len(symbols):
            raise ValueError("pilot universe must contain one to five unique symbols")
        return tuple(self.resolve_nse_equity(symbol, cache_path=cache_path) for symbol in symbols)

    def fetch_month(
        self, instrument_key: str, start: date | str, end: date | str,
        *, raw_cache_path: str | Path | None = None,
    ) -> tuple[list[MarketBar], dict[str, Any]]:
        first, last = _utc_date(start), _utc_date(end)
        if (first.year, first.month) != (last.year, last.month):
            raise ValueError("fetch_month accepts dates within one calendar month")
        if first < date(2022, 1, 1):
            raise ValueError("Upstox V3 minute history is documented from January 2022")
        if not instrument_key.startswith("NSE_EQ|"):
            raise ValueError("only resolved NSE cash-equity instrument keys are accepted")
        safe_key = quote(instrument_key, safe="")
        url = f"{API_ROOT}/historical-candle/{safe_key}/minutes/1/{last.isoformat()}/{first.isoformat()}"
        cache = Path(raw_cache_path) if raw_cache_path else None
        if cache and cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProviderError("cached Upstox month payload is incomplete or invalid") from exc
        else:
            payload = self._get_json(url, headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
            if cache:
                _atomic_write(cache, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
        if payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
            raise ProviderError("Upstox historical response did not report success")
        rows = payload["data"].get("candles")
        if not isinstance(rows, list):
            raise ProviderError("Upstox historical response has no candle list")
        bars: list[MarketBar] = []
        seen: set[datetime] = set()
        previous: datetime | None = None
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) < 6:
                raise ProviderError(f"Upstox candle row {index} has an invalid shape")
            raw_stamp = str(row[0])
            try:
                stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError("timezone missing")
                stamp = stamp.astimezone(UTC)
                bar = MarketBar(
                    timestamp=stamp, instrument_id=instrument_key,
                    open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5],
                    source_id="upstox:v3", source_timestamp=raw_stamp,
                    base_interval_minutes=1, exchange_local_timestamp=raw_stamp,
                )
            except (ValueError, TypeError, IndexError) as exc:
                raise ProviderError(f"Upstox candle row {index} failed validation: {type(exc).__name__}") from None
            if stamp.date() < first or stamp.date() > last:
                raise ProviderError(f"Upstox returned candle outside requested range at row {index}")
            if stamp in seen:
                raise ProviderError(f"Upstox returned duplicate candle timestamp at row {index}")
            if previous is not None and stamp < previous:
                raise ProviderError(f"Upstox returned out-of-order candle at row {index}")
            seen.add(stamp)
            previous = stamp
            bars.append(bar)
        return bars, payload

    def fetch_range(self, instrument_key: str, start: date | str, end: date | str,
                    *, raw_cache_dir: str | Path | None = None):
        """Fetch inclusive monthly chunks, preserving chunk payloads for immutable raw storage."""
        result = []
        for first, last in month_chunks(start, end):
            cache = None
            if raw_cache_dir:
                key_hash = hashlib.sha256(instrument_key.encode()).hexdigest()[:12]
                cache = Path(raw_cache_dir) / key_hash / f"{first:%Y-%m}.json"
            result.append((first, last, *self.fetch_month(
                instrument_key, first, last, raw_cache_path=cache)))
        return result


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
