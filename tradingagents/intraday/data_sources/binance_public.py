"""Read-only Binance public SPOT monthly archive client (never futures)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Any

import requests

from tradingagents.intraday.data_sources.common import ProviderError
from tradingagents.intraday.models import MarketBar

TERMS_URL = "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
DATA_ROOT = "https://data.binance.vision/data/spot/monthly/klines"


def verify_sha256(content: bytes, checksum_text: str) -> str:
    """Verify the archive against a Binance checksum sidecar."""
    match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
    if not match:
        raise ProviderError("Binance checksum sidecar has no SHA-256 digest")
    actual = hashlib.sha256(content).hexdigest()
    if actual.lower() != match.group(1).lower():
        raise ProviderError("Binance archive checksum mismatch")
    return actual


def binance_spot_timestamp(value: int, archive_month: str) -> datetime:
    """Decode spot open-time units: milliseconds through 2024, microseconds from 2025."""
    year = int(archive_month[:4])
    try:
        seconds = value / (1_000_000 if year >= 2025 else 1_000)
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ProviderError("Binance candle timestamp is invalid") from exc


class BinanceSpotArchiveClient:
    def __init__(self, *, session: Any | None = None):
        self.session = session or requests.Session()

    def fetch_monthly_klines(self, symbol: str, month: str, *, terms_acknowledged: bool = False):
        if not terms_acknowledged:
            raise ProviderError("Binance dataset terms must be acknowledged before download/use")
        if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
            raise ValueError("symbol must be a Binance spot symbol, such as BTCUSDT")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must use YYYY-MM")
        base = f"{DATA_ROOT}/{symbol}/1m/{symbol}-1m-{month}"
        archive_response = self.session.get(base + ".zip", timeout=90)
        sidecar_response = self.session.get(base + ".zip.CHECKSUM", timeout=30)
        if archive_response.status_code != 200 or sidecar_response.status_code != 200:
            raise ProviderError("Binance public archive or checksum is unavailable")
        digest = verify_sha256(archive_response.content, sidecar_response.text)
        try:
            with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
                if len(names) != 1:
                    raise ProviderError("Binance archive must contain exactly one kline CSV")
                rows = csv.reader(io.TextIOWrapper(archive.open(names[0]), encoding="utf-8"))
                result = []
                seen = set()
                previous = None
                for rownum, row in enumerate(rows):
                    if rownum == 0 and row and not row[0].lstrip("-+").isdigit():
                        continue
                    if len(row) < 6:
                        raise ProviderError(f"Binance kline row {rownum} is malformed")
                    raw_ts = int(row[0])
                    stamp = binance_spot_timestamp(raw_ts, month)
                    if stamp.strftime("%Y-%m") != month:
                        raise ProviderError(f"Binance row {rownum} timestamp is outside the requested month")
                    if stamp in seen or previous is not None and stamp < previous:
                        raise ProviderError("Binance archive contains duplicate or out-of-order bars")
                    bar = MarketBar(stamp, f"BINANCE_SPOT:{symbol}", row[1], row[2], row[3], row[4], row[5], "binance:spot:monthly-klines", str(raw_ts))
                    result.append(bar)
                    seen.add(stamp)
                    previous = stamp
        except (zipfile.BadZipFile, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(f"Binance archive could not be parsed: {type(exc).__name__}") from None
        return result, {
            "archive_url": base + ".zip", "sha256": digest, "terms_url": TERMS_URL,
            "raw_archive_bytes": archive_response.content,
            "raw_checksum_bytes": sidecar_response.content or sidecar_response.text.encode(),
        }

    def fetch_monthly_aggtrades_archive(self, symbol: str, month: str, *, terms_acknowledged: bool = False):
        """Return verified SPOT aggTrades archive bytes for later trade-level parsing."""
        if not terms_acknowledged:
            raise ProviderError("Binance dataset terms must be acknowledged before download/use")
        if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol) or not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("invalid spot symbol or YYYY-MM month")
        base = f"https://data.binance.vision/data/spot/monthly/aggTrades/{symbol}/{symbol}-aggTrades-{month}"
        archive = self.session.get(base + ".zip", timeout=90)
        checksum = self.session.get(base + ".zip.CHECKSUM", timeout=30)
        if archive.status_code != 200 or checksum.status_code != 200:
            raise ProviderError("Binance spot aggTrades archive is unavailable")
        digest = verify_sha256(archive.content, checksum.text)
        return archive.content, {
            "archive_url": base + ".zip", "sha256": digest, "terms_url": TERMS_URL,
            "raw_archive_bytes": archive.content,
            "raw_checksum_bytes": checksum.content or checksum.text.encode(),
        }
