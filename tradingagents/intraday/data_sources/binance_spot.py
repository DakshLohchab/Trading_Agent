"""Binance Spot API client for live market data, exchange filters, and trade execution."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests

from tradingagents.intraday.crypto_exchange import (
    BinanceSpotFilters,
    parse_binance_spot_exchange_info,
)
from tradingagents.intraday.data_sources.common import ProviderError, RateLimiter, retry_call
from tradingagents.intraday.models import MarketBar

D = Decimal
BINANCE_API_BASE = "https://api.binance.com"
BINANCE_TESTNET_BASE = "https://testnet.binance.vision"


class BinanceSpotClient:
    """Binance Spot REST client supporting both public market data and signed trading endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        testnet: bool = False,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("BINANCE_API_KEY")
        self.api_secret = api_secret or os.environ.get("BINANCE_API_SECRET")
        self.base_url = BINANCE_TESTNET_BASE if testnet else BINANCE_API_BASE
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter(requests_per_second=10.0)

    # -------------------------------------------------------------------------
    # Public Market Data Endpoints
    # -------------------------------------------------------------------------

    def get_ticker_price(self, symbol: str = "BTCUSDT") -> Decimal:
        """Fetch current spot price for symbol."""
        endpoint = f"{self.base_url}/api/v3/ticker/price"
        data = self._request("GET", endpoint, params={"symbol": symbol})
        return D(str(data["price"]))

    def get_order_book(self, symbol: str = "BTCUSDT", limit: int = 10) -> dict[str, Any]:
        """Fetch current order book depth."""
        endpoint = f"{self.base_url}/api/v3/depth"
        data = self._request("GET", endpoint, params={"symbol": symbol, "limit": limit})
        return {
            "bids": [(D(p), D(q)) for p, q in data.get("bids", [])],
            "asks": [(D(p), D(q)) for p, q in data.get("asks", [])],
        }

    def get_recent_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        limit: int = 500,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[MarketBar]:
        """Fetch recent completed klines and convert to canonical MarketBar instances.

        Binance Kline array structure:
        [
            0: Open time (ms),
            1: Open,
            2: High,
            3: Low,
            4: Close,
            5: Volume,
            6: Close time (ms),
            ...
        ]
        """
        endpoint = f"{self.base_url}/api/v3/klines"
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        raw_klines = self._request("GET", endpoint, params=params)
        bars: list[MarketBar] = []

        now_ms = int(time.time() * 1000)
        interval_minutes = 1 if interval == "1m" else 5 if interval == "5m" else 15

        for row in raw_klines:
            open_time_ms = int(row[0])
            close_time_ms = int(row[6])

            # Omit candle if it is currently still forming (unless looking at historical)
            if close_time_ms > now_ms and len(raw_klines) > 1 and row == raw_klines[-1]:
                continue

            open_dt = datetime.fromtimestamp(open_time_ms / 1000.0, tz=UTC)
            source_id = f"binance:spot:{symbol}:{interval}:{open_time_ms}"

            bar = MarketBar(
                timestamp=open_dt,
                instrument_id=f"BINANCE_SPOT:{symbol}",
                open=D(str(row[1])),
                high=D(str(row[2])),
                low=D(str(row[3])),
                close=D(str(row[4])),
                volume=D(str(row[5])),
                source_id=source_id,
                source_timestamp=str(open_time_ms),
                base_interval_minutes=interval_minutes,
                quality="observed",
            )
            bars.append(bar)

        return bars

    def get_exchange_filters(
        self,
        symbol: str = "BTCUSDT",
        *,
        cache_path: str | None = None,
    ) -> BinanceSpotFilters:
        """Fetch, parse, and validate Binance Spot exchangeInfo rules for symbol."""
        endpoint = f"{self.base_url}/api/v3/exchangeInfo"
        raw_bytes = self._request_raw("GET", endpoint, params={"symbol": symbol})

        now_utc = datetime.now(UTC)
        filters = parse_binance_spot_exchange_info(
            raw_bytes,
            source_endpoint=f"{endpoint}?symbol={symbol}",
            retrieved_at=now_utc,
        )

        if cache_path:
            try:
                import json
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(filters.as_record(), f, indent=2)
            except OSError:
                pass

        return filters

    # -------------------------------------------------------------------------
    # Authenticated Trading Endpoints
    # -------------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """Fetch authenticated account info (balances, permissions)."""
        self._require_auth()
        endpoint = f"{self.base_url}/api/v3/account"
        return self._request_signed("GET", endpoint)

    def get_asset_balance(self, asset: str = "USDT") -> Decimal:
        """Fetch free balance for specified asset."""
        account = self.get_account()
        balances = account.get("balances", [])
        for b in balances:
            if b.get("asset") == asset:
                return D(str(b.get("free", "0")))
        return D(0)

    def test_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        time_in_force: str = "GTC",
    ) -> dict[str, Any]:
        """Validate an order against exchange filters without actually executing it."""
        self._require_auth()
        endpoint = f"{self.base_url}/api/v3/order/test"
        params = self._build_order_params(symbol, side, order_type, quantity, price, time_in_force)
        return self._request_signed("POST", endpoint, data=params)

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        time_in_force: str = "GTC",
        new_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a live order on Binance Spot."""
        self._require_auth()
        endpoint = f"{self.base_url}/api/v3/order"
        params = self._build_order_params(symbol, side, order_type, quantity, price, time_in_force)
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        return self._request_signed("POST", endpoint, data=params)

    def cancel_order(self, symbol: str, order_id: int | str) -> dict[str, Any]:
        """Cancel an open order."""
        self._require_auth()
        endpoint = f"{self.base_url}/api/v3/order"
        params = {"symbol": symbol, "orderId": order_id}
        return self._request_signed("DELETE", endpoint, data=params)

    def get_order(self, symbol: str, order_id: int | str) -> dict[str, Any]:
        """Check status of an order."""
        self._require_auth()
        endpoint = f"{self.base_url}/api/v3/order"
        params = {"symbol": symbol, "orderId": order_id}
        return self._request_signed("GET", endpoint, params=params)

    # -------------------------------------------------------------------------
    # Internal Request & Signing Helpers
    # -------------------------------------------------------------------------

    def _require_auth(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ProviderError(
                "Binance API credentials missing. Set BINANCE_API_KEY and BINANCE_API_SECRET in your environment."
            )

    def _build_order_params(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        time_in_force: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
        }
        if order_type.upper() in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
            if price is None:
                raise ValueError("Price is required for LIMIT orders")
            params["price"] = f"{price:.2f}"
            params["timeInForce"] = time_in_force
        return params

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(query)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query["signature"] = signature
        return query

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.rate_limiter.wait()
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

        def call():
            resp = self.session.request(method, endpoint, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                raise ProviderError(f"Binance rate limit exceeded: {resp.text}")
            if resp.status_code >= 400:
                raise ProviderError(f"Binance API error {resp.status_code}: {resp.text}")
            return resp.json()

        return retry_call(call, attempts=3, retryable=lambda e: "rate limit" not in str(e).lower())

    def _request_raw(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> bytes:
        self.rate_limiter.wait()
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

        def call():
            resp = self.session.request(method, endpoint, params=params, headers=headers, timeout=15)
            if resp.status_code >= 400:
                raise ProviderError(f"Binance API error {resp.status_code}: {resp.text}")
            return resp.content

        return retry_call(call, attempts=3, retryable=lambda e: True)

    def _request_signed(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        self.rate_limiter.wait()
        headers = {"X-MBX-APIKEY": self.api_key}

        target = data if method in ("POST", "DELETE") else params
        payload = self._sign_params(target or {})

        def call():
            if method in ("POST", "DELETE"):
                resp = self.session.request(method, endpoint, data=payload, headers=headers, timeout=15)
            else:
                resp = self.session.request(method, endpoint, params=payload, headers=headers, timeout=15)
            if resp.status_code >= 400:
                raise ProviderError(f"Binance signed API error {resp.status_code}: {resp.text}")
            return resp.json()

        return retry_call(call, attempts=3, retryable=lambda e: False)
