"""Read-only Alpaca historical bars pinned to the IEX feed."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import requests

from tradingagents.intraday.data_sources.common import ProviderError, RateLimiter, retry_call
from tradingagents.intraday.models import MarketBar

API_ROOT = "https://data.alpaca.markets/v2/stocks"


class _Failure(Exception):
    def __init__(self, status: int, retry_after: str | None = None):
        self.status, self.retry_after = status, retry_after


class AlpacaIEXClient:
    """Historical stock-data interface; feed is intentionally fixed to IEX."""

    def __init__(self, *, key: str | None = None, secret: str | None = None,
                 session: Any | None = None, rate_limiter: RateLimiter | None = None,
                 sleep=time.sleep, attempts: int = 5):
        self.key, self.secret = key, secret
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter(5.0)
        self.sleep, self.attempts = sleep, attempts

    def fetch_bars(self, symbol: str, start: str, end: str) -> list[MarketBar]:
        key = self.key or os.environ.get("APCA_API_KEY_ID")
        secret = self.secret or os.environ.get("APCA_API_SECRET_KEY")
        if not key or not secret:
            raise ProviderError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
        if not symbol or any(c in symbol for c in "/?&#"):
            raise ValueError("symbol must be a plain stock ticker")
        bars: list[MarketBar] = []
        page_token: str | None = None
        seen: set[datetime] = set()
        previous: datetime | None = None
        while True:
            params = {"timeframe": "1Min", "feed": "iex", "start": start, "end": end, "limit": 10000}
            if page_token:
                params["page_token"] = page_token
            def request(params=params):
                self.rate_limiter.wait()
                response = self.session.get(
                    f"{API_ROOT}/{symbol.upper()}/bars", params=params,
                    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=30,
                )
                if response.status_code >= 400:
                    raise _Failure(response.status_code, response.headers.get("Retry-After"))
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError("Alpaca returned invalid JSON") from exc
            try:
                payload = retry_call(
                    request, attempts=self.attempts, sleep=self.sleep,
                    retryable=lambda e: isinstance(e, requests.RequestException) or isinstance(e, _Failure) and (e.status == 429 or e.status >= 500),
                    retry_after=lambda e: e.retry_after if isinstance(e, _Failure) else None,
                )
            except _Failure as exc:
                raise ProviderError(f"Alpaca request failed with HTTP {exc.status}") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
                raise ProviderError("Alpaca response has no bars list")
            for row in payload["bars"]:
                try:
                    raw = str(row["t"])
                    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("timezone missing")
                    stamp = stamp.astimezone(UTC)
                    bar = MarketBar(stamp, f"{symbol.upper()}.IEX", row["o"], row["h"], row["l"], row["c"], row["v"], "alpaca:iex", raw)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderError(f"Alpaca bar failed validation: {type(exc).__name__}") from None
                if stamp in seen or previous is not None and stamp < previous:
                    raise ProviderError("Alpaca returned duplicate or out-of-order bars")
                seen.add(stamp)
                previous = stamp
                bars.append(bar)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return bars
