"""Analyst types and utilities for TradingAgents."""

from enum import StrEnum


class AnalystType(StrEnum):
    MARKET = "market"
    SOCIAL = "social"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"


class AssetType(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"


CRYPTO_SUFFIXES = (
    "-USD",
    "-USDT",
    "-USDC",
    "-EUR",
    "-GBP",
    "-BTC",
    "-ETH",
)


def detect_asset_type(ticker: str) -> AssetType:
    """Classify on the canonical symbol so e.g. BTCUSD and BTC-USDT both read as
    crypto, matching what the data path will actually fetch.
    """
    from tradingagents.dataflows.symbols import normalize_symbol

    try:
        canonical = normalize_symbol(ticker)
    except Exception:
        canonical = ticker.strip().upper()

    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    return AssetType.STOCK


def filter_analysts_for_asset_type(
    analysts: list[AnalystType | str], asset_type: AssetType | str
) -> list[AnalystType | str]:
    """Filter out fundamentals analyst when analyzing crypto assets."""
    target_asset = AssetType(asset_type) if isinstance(asset_type, str) else asset_type
    if target_asset == AssetType.CRYPTO:
        return [
            a for a in analysts
            if (a.value if isinstance(a, AnalystType) else a) != AnalystType.FUNDAMENTALS.value
        ]
    return list(analysts)


__all__ = [
    "AnalystType",
    "AssetType",
    "CRYPTO_SUFFIXES",
    "detect_asset_type",
    "filter_analysts_for_asset_type",
]
