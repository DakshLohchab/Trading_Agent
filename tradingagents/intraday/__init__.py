"""Isolated, deterministic historical market-data and replay components."""

from tradingagents.intraday.models import (
    CurrencyConversionMetadata,
    InstrumentMetadata,
    MarketBar,
    ReplayEvent,
)

__all__ = [
    "CurrencyConversionMetadata",
    "InstrumentMetadata",
    "MarketBar",
    "ReplayEvent",
]


def __getattr__(name: str):
    if name == "CryptoDayTrader":
        from tradingagents.intraday.crypto_day_trader import CryptoDayTrader
        return CryptoDayTrader
    if name == "BinanceSpotClient":
        from tradingagents.intraday.data_sources.binance_spot import BinanceSpotClient
        return BinanceSpotClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
