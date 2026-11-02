"""NautilusTrader 1.231.0-only conversion and simulation helpers.

This module imports NautilusTrader lazily so the base TradingAgents environment
does not need NautilusTrader installed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata

SUPPORTED_NAUTILUS_VERSION = "1.231.0"


def _nautilus_imports():
    try:
        from importlib.metadata import version

        from nautilus_trader.backtest.config import BacktestEngineConfig
        from nautilus_trader.backtest.engine import BacktestEngine
        from nautilus_trader.model.currencies import Currency
        from nautilus_trader.model.data import Bar, BarSpecification, BarType
        from nautilus_trader.model.enums import (
            AccountType,
            AggregationSource,
            BarAggregation,
            OmsType,
            PriceType,
        )
        from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
        from nautilus_trader.model.instruments import CurrencyPair, Equity
        from nautilus_trader.model.objects import Money, Price, Quantity
        from nautilus_trader.trading.strategy import Strategy
    except ImportError as exc:
        raise RuntimeError(
            "NautilusTrader must be installed in the isolated .venv-nautilus environment"
        ) from exc
    actual = version("nautilus_trader")
    if actual != SUPPORTED_NAUTILUS_VERSION:
        raise RuntimeError(
            f"expected NautilusTrader {SUPPORTED_NAUTILUS_VERSION}, found {actual}; 2.x is not supported here"
        )
    return {
        "BacktestEngineConfig": BacktestEngineConfig,
        "BacktestEngine": BacktestEngine,
        "Currency": Currency,
        "Bar": Bar,
        "BarSpecification": BarSpecification,
        "BarType": BarType,
        "AccountType": AccountType,
        "AggregationSource": AggregationSource,
        "BarAggregation": BarAggregation,
        "OmsType": OmsType,
        "PriceType": PriceType,
        "InstrumentId": InstrumentId,
        "Symbol": Symbol,
        "Venue": Venue,
        "Equity": Equity,
        "CurrencyPair": CurrencyPair,
        "Money": Money,
        "Price": Price,
        "Quantity": Quantity,
        "Strategy": Strategy,
    }


def _ns(timestamp):
    return int(timestamp.timestamp() * 1_000_000_000)


def _price_text(value: Decimal, precision: int) -> str:
    return f"{value:.{precision}f}"


def run_bar_simulation(
    instrument: InstrumentMetadata,
    candles: list[AggregatedCandle] | tuple[AggregatedCandle, ...],
    *,
    crypto_account=None,
) -> dict:
    """Run a no-orders historical data-delivery simulation.

    Crypto spot requires an explicit isolated virtual account; it is never routed
    through a margin venue. Equity venue/account initialization stays unchanged.
    """
    if instrument.asset_class == "crypto_spot":
        if instrument.session_calendar != "24/7":
            raise ValueError("crypto spot requires the continuous 24/7 market calendar")
        if crypto_account is None:
            raise ValueError("crypto spot data simulation requires explicit virtual account balances")
        if crypto_account.account_currency != instrument.quote_currency:
            raise ValueError("crypto virtual account currency must match the quote currency")
    elif crypto_account is not None:
        raise ValueError("crypto_account is only valid for crypto spot instruments")
    api = _nautilus_imports()
    if not candles or any(not candle.is_valid for candle in candles):
        raise ValueError(
            "simulation requires at least one valid completed candle and no invalid candles"
        )
    ids = [c.instrument_id for c in candles]
    timestamps = [c.timestamp for c in candles]
    intervals = {c.interval_minutes for c in candles}
    if (
        any(identifier != instrument.instrument_id for identifier in ids)
        or timestamps != sorted(timestamps)
        or len(set(timestamps)) != len(timestamps)
        or len(intervals) != 1
        or any(
            c.end_timestamp - c.timestamp != timedelta(minutes=c.interval_minutes) for c in candles
        )
    ):
        raise ValueError(
            "simulation bars must be unique, chronological, and belong to the selected instrument"
        )
    (
        BacktestEngineConfig,
        BacktestEngine,
        Currency,
        Bar,
        BarSpecification,
        BarType,
        AccountType,
        AggregationSource,
        BarAggregation,
        OmsType,
        PriceType,
        InstrumentId,
        Symbol,
        Venue,
        Equity,
        CurrencyPair,
        Money,
        Price,
        Quantity,
        Strategy,
    ) = (
        api[k]
        for k in (
            "BacktestEngineConfig",
            "BacktestEngine",
            "Currency",
            "Bar",
            "BarSpecification",
            "BarType",
            "AccountType",
            "AggregationSource",
            "BarAggregation",
            "OmsType",
            "PriceType",
            "InstrumentId",
            "Symbol",
            "Venue",
            "Equity",
            "CurrencyPair",
            "Money",
            "Price",
            "Quantity",
            "Strategy",
        )
    )
    try:
        instrument_id = InstrumentId.from_str(instrument.instrument_id)
    except Exception as exc:
        raise ValueError("Nautilus instrument_id must use its canonical SYMBOL.VENUE form") from exc
    venue_code = instrument_id.venue.value
    if instrument.venue.upper() not in (
        venue_code.upper(),
        "NASDAQ"
        if venue_code == "XNAS"
        else "NYSE"
        if venue_code == "XNYS"
        else venue_code.upper(),
    ):
        raise ValueError("instrument venue metadata conflicts with canonical instrument_id venue")
    quote_currency = Currency.from_str(instrument.quote_currency.upper())
    price_increment = Price.from_str(str(instrument.tick_size))
    lot_size = Quantity.from_str(str(instrument.quantity_increment))
    min_quantity = (
        Quantity.from_str(str(instrument.min_order_quantity))
        if instrument.min_order_quantity
        else None
    )
    if instrument.asset_class == "equity":
        nautilus_instrument = Equity(
            instrument_id=instrument_id,
            raw_symbol=Symbol(instrument.symbol),
            currency=quote_currency,
            price_precision=instrument.price_precision,
            price_increment=price_increment,
            lot_size=lot_size,
            ts_event=0,
            ts_init=0,
            min_quantity=min_quantity,
        )
    else:
        base_currency = Currency.from_str(instrument.base_currency.upper())
        quantity_precision = max(0, -instrument.quantity_increment.normalize().as_tuple().exponent)
        nautilus_instrument = CurrencyPair(
            instrument_id=instrument_id,
            raw_symbol=Symbol(instrument.symbol),
            base_currency=base_currency,
            quote_currency=quote_currency,
            price_precision=instrument.price_precision,
            size_precision=quantity_precision,
            price_increment=price_increment,
            size_increment=lot_size,
            lot_size=lot_size,
            ts_event=0,
            ts_init=0,
            min_quantity=min_quantity,
            min_notional=(
                Money(instrument.min_order_notional, quote_currency)
                if instrument.min_order_notional
                else None
            ),
        )
    bar_type = BarType(
        instrument_id,
        BarSpecification(candles[0].interval_minutes, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    nautilus_bars = [
        Bar(
            bar_type,
            Price.from_str(_price_text(candle.open, instrument.price_precision)),
            Price.from_str(_price_text(candle.high, instrument.price_precision)),
            Price.from_str(_price_text(candle.low, instrument.price_precision)),
            Price.from_str(_price_text(candle.close, instrument.price_precision)),
            Quantity.from_str(str(candle.volume)),
            _ns(candle.end_timestamp),
            _ns(candle.end_timestamp),
        )
        for candle in candles
    ]
    delivered = []

    class DataCapture(Strategy):
        def on_start(self):
            self.subscribe_bars(bar_type)

        def on_bar(self, bar):
            delivered.append(
                (
                    bar.ts_event,
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                )
            )

    engine = BacktestEngine(BacktestEngineConfig(run_analysis=False))
    if instrument.asset_class == "crypto_spot":
        starting_balances = [
            Money(crypto_account.starting_quote_balance, quote_currency),
            Money(crypto_account.starting_base_balance, Currency.from_str(instrument.base_currency.upper())),
        ]
        account_type = AccountType.CASH
        account_base_currency = None
    else:
        starting_balances = [Money(Decimal("1000000"), quote_currency)]
        account_type = AccountType.CASH
        account_base_currency = quote_currency
    try:
        engine.add_venue(
            Venue(venue_code),
            OmsType.NETTING,
            account_type,
            starting_balances,
            base_currency=account_base_currency,
            **({"allow_cash_borrowing": False} if instrument.asset_class == "crypto_spot" else {}),
        )
        engine.add_instrument(nautilus_instrument)
        engine.add_data(nautilus_bars)
        engine.add_strategy(DataCapture())
        engine.run()
    finally:
        engine.dispose()
    expected = [bar.ts_event for bar in nautilus_bars]
    if [row[0] for row in delivered] != expected:
        raise RuntimeError("NautilusTrader did not deliver the expected historical bars in order")
    return {
        "nautilus_version": SUPPORTED_NAUTILUS_VERSION,
        "instrument_id": str(instrument_id),
        "venue": venue_code,
        "account_type": account_type.name,
        "account_currency": crypto_account.account_currency if crypto_account else instrument.quote_currency,
        "bar_type": str(bar_type),
        "delivered_bars": tuple(delivered),
        "bar_count": len(delivered),
    }
