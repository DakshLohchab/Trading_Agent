"""Simulation-only BTC spot candidate-to-fill adapter for NautilusTrader 1.231.0."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace

SUPPORTED_VERSION = "1.231.0"
ROOT = Path(__file__).resolve().parents[2]
NAUTILUS_PYTHON = ROOT / ".venv-nautilus" / "bin" / "python"


def run_crypto_nautilus_execution(
    instrument,
    candles,
    candidates,
    *,
    execution_spec,
    account,
    fee_configuration,
    costs,
    run_id: str,
) -> dict:
    """Run synthetic or future historical inputs; all orders are internal simulation orders."""
    if not NAUTILUS_PYTHON.is_file():
        raise RuntimeError(f"NautilusTrader environment not found at {NAUTILUS_PYTHON}")
    if not candles or any(
        getattr(candle, "is_valid", True) is not True
        or getattr(candle, "quality", "observed") != "observed"
        for candle in candles
    ):
        raise ValueError("crypto execution requires valid observed completed candles")
    if instrument.asset_class != "crypto_spot" or instrument.session_calendar != "24/7":
        raise ValueError("crypto execution requires an explicit 24/7 spot instrument")
    if execution_spec.exchange_filters is None:
        raise ValueError("current Binance Spot filters are required for crypto execution")
    if fee_configuration is None or costs is None or not costs.available:
        raise ValueError("fee, spread, and slippage configuration is required")
    if fee_configuration.fee_currency != instrument.quote_currency:
        raise ValueError("Nautilus 1.231.0 integration currently requires quote-currency fees")
    if costs.fee_model != fee_configuration.taker_model():
        raise ValueError("execution cost fee model must match the configured taker rate")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("duplicate candidate IDs are refused before Nautilus order submission")
    def bar_end(item):
        return (
            item.end_timestamp
            if hasattr(item, "end_timestamp")
            else item.timestamp + timedelta(minutes=item.base_interval_minutes)
        )

    def bar_interval(item):
        return item.interval_minutes if hasattr(item, "interval_minutes") else item.base_interval_minutes

    def bar_sources(item):
        return item.source_ids if hasattr(item, "source_ids") else (item.source_id,)

    ordered = sorted(candles, key=bar_end)
    if len({bar_end(item) for item in ordered}) != len(ordered):
        raise ValueError("execution candles must have unique completion timestamps")
    if any(item.instrument_id != instrument.instrument_id for item in ordered):
        raise ValueError("execution candle instrument IDs must match the selected instrument")
    if any(
        bar_end(item) - item.timestamp != timedelta(minutes=bar_interval(item))
        for item in ordered
    ) or len({bar_interval(item) for item in ordered}) != 1:
        raise ValueError("execution candles must be complete and use one timeframe")
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.timestamp != bar_end(previous):
            raise ValueError("crypto execution event stream contains missing or overlapping candles")
    event_ends = {bar_end(item) for item in ordered}
    for candidate in candidates:
        if candidate.decision_timestamp not in event_ends:
            raise ValueError("candidate decision must align with a completed execution event")
        if candidate.decision_timestamp.minute % 5 or candidate.decision_timestamp.second:
            raise ValueError("crypto VWAP candidates must be emitted from completed 5-minute bars")
        if candidate.reference_candle_timestamp + timedelta(minutes=5) != candidate.decision_timestamp:
            raise ValueError("candidate reference must be its completed 5-minute decision candle")
        if candidate.strategy_id != "crypto_vwap_trend_pullback" or not candidate.strategy_version:
            raise ValueError("only versioned crypto VWAP candidates can enter this worker")
        snapshot = dict(candidate.indicator_snapshot)
        confirmation = snapshot.get("confirmation_available_at")
        if confirmation is None:
            raise ValueError("candidate is missing its 15-minute confirmation availability timestamp")
        if datetime.fromisoformat(confirmation) > candidate.decision_timestamp:
            raise ValueError("candidate contains a future 15-minute confirmation")
        vwap_method = snapshot.get(
            "5m.reclaim.vwap_method",
            snapshot.get("5m.reclaim.vwap.method", snapshot.get("vwap_method")),
        )
        if vwap_method != "ohlcv_typical_price_approximation":
            raise ValueError("candidate must identify OHLCV-approximate VWAP semantics")
        if snapshot.get("strategy_config.crypto_session_anchor_utc") != "00:00:00":
            raise ValueError("candidate must record its 00:00 UTC VWAP reset")
        if (
            snapshot.get("crypto_strategy.price_input")
            != "ohlcv_typical_price=(high+low+close)/3"
            or snapshot.get("crypto_strategy.volume_input") != "binance_kline_base_volume"
            or snapshot.get("crypto_strategy.candle_completion") != "bar_end_timestamp"
        ):
            raise ValueError("candidate is missing the crypto VWAP input and completion semantics")
    payload = {
        "nautilus_version": SUPPORTED_VERSION,
        "run_id": run_id,
        "instrument": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(instrument).items()
        },
        "filters": execution_spec.exchange_filters.as_record(),
        "account": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(account).items()
        },
        "fees": {
            "maker_rate": str(fee_configuration.maker_rate),
            "taker_rate": str(fee_configuration.taker_rate),
            "fee_currency": fee_configuration.fee_currency,
            "source": fee_configuration.source,
            "configured_at": fee_configuration.configured_at.isoformat(),
            "actual_account_rates": fee_configuration.actual_account_rates,
            "version": fee_configuration.version,
        },
        "costs": {
            "slippage_bps": str(costs.slippage_bps),
            "spread_bps": str(costs.spread_bps),
            "source": costs.source,
            "inputs_verified": costs.inputs_verified,
            "version": costs.version,
        },
        "candles": [
            {
                "timestamp": item.timestamp.isoformat(),
                "end_timestamp": bar_end(item).isoformat(),
                "instrument_id": item.instrument_id,
                "interval_minutes": bar_interval(item),
                "open": str(item.open), "high": str(item.high), "low": str(item.low),
                "close": str(item.close), "volume": str(item.volume),
                "source_ids": list(bar_sources(item)),
            }
            for item in ordered
        ],
        "candidates": [_candidate_record(item) for item in candidates],
    }
    payload["input_market_data_checksum"] = hashlib.sha256(
        json.dumps(payload["candles"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = subprocess.run(
        [str(NAUTILUS_PYTHON), str(Path(__file__).resolve()), "--worker"],
        input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        text=True, capture_output=True, check=False, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    if result.returncode:
        raise RuntimeError(f"Nautilus crypto worker failed: {result.stderr[-4000:]}")
    line = next((line for line in reversed(result.stdout.splitlines()) if line.startswith("CRYPTO_RESULT=")), None)
    if line is None:
        raise RuntimeError(f"Nautilus crypto worker returned no result: {result.stderr[-2000:]}")
    output = json.loads(line.removeprefix("CRYPTO_RESULT="))
    output["input_market_data_checksum"] = payload["input_market_data_checksum"]
    output["cost_status"] = costs.cost_status
    output["realistic_net_performance"] = False
    output["fill_limitations"] = [
        "OHLCV only: no historical order book, observed spread, queue position, or market depth.",
        "OHLCV does not preserve intraminute trade order; when stop and target are both touched, stop wins.",
        "Configured spread and slippage adjust fills from Nautilus execution-event references; historical book depth is unavailable.",
    ]
    return output


def _candidate_record(candidate) -> dict:
    item = asdict(candidate)
    for key, value in tuple(item.items()):
        if isinstance(value, (datetime, Decimal)):
            item[key] = value.isoformat() if isinstance(value, datetime) else str(value)
    item["market_regime"] = candidate.market_regime.value
    item["status"] = candidate.status.value
    return item


def _worker(payload: dict) -> dict:
    import types
    from collections import deque

    root = Path(__file__).resolve().parents[2]
    package = types.ModuleType("tradingagents")
    package.__path__ = [str(root / "tradingagents")]
    package.__package__ = "tradingagents"
    sys.modules.setdefault("tradingagents", package)
    sub = types.ModuleType("tradingagents.intraday")
    sub.__path__ = [str(root / "tradingagents" / "intraday")]
    sub.__package__ = "tradingagents.intraday"
    sys.modules.setdefault("tradingagents.intraday", sub)

    from nautilus_trader import __version__ as actual_version
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.currencies import Currency
    from nautilus_trader.model.data import Bar, BarSpecification, BarType, BookOrder
    from nautilus_trader.model.enums import (
        AccountType,
        AggregationSource,
        BarAggregation,
        BookType,
        OmsType,
        OrderSide,
        PriceType,
        TimeInForce,
        TriggerType,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    from tradingagents.intraday.candidates import CandidateStatus, TradeCandidate
    from tradingagents.intraday.crypto_costs import CryptoExecutionCostModel
    from tradingagents.intraday.crypto_exchange import BinanceSpotFilters
    from tradingagents.intraday.crypto_execution import (
        CryptoMarketDataState,
        CryptoSpotAccountConfig,
        CryptoSpotAccountLedger,
        CryptoSpotCostAssumptions,
        CryptoSpotExecutionSpec,
        CryptoSpotFeeConfiguration,
        approve_crypto_candidate,
    )
    from tradingagents.intraday.models import InstrumentMetadata
    from tradingagents.intraday.regimes import MarketRegime

    if actual_version != SUPPORTED_VERSION or payload["nautilus_version"] != SUPPORTED_VERSION:
        raise RuntimeError(f"crypto worker requires NautilusTrader {SUPPORTED_VERSION}; got {actual_version}")
    D = Decimal
    instrument_values = dict(payload["instrument"])
    for name in ("tick_size", "quantity_increment", "min_order_quantity", "min_order_notional"):
        if instrument_values.get(name) is not None:
            instrument_values[name] = D(instrument_values[name])
    instrument = InstrumentMetadata(**instrument_values)
    filter_values = dict(payload["filters"])
    for name, value in tuple(filter_values.items()):
        if name.endswith("_utc") and value is not None:
            filter_values[name] = datetime.fromisoformat(value)
        elif name in {
            "tick_size", "min_price", "max_price", "lot_step_size", "lot_min_quantity",
            "lot_max_quantity", "market_step_size", "market_min_quantity", "market_max_quantity",
            "min_notional", "max_notional", "bid_multiplier_down", "bid_multiplier_up",
            "ask_multiplier_down", "ask_multiplier_up",
        } and value is not None:
            filter_values[name] = D(value)
    filters = BinanceSpotFilters(**filter_values)
    account_values = dict(payload["account"])
    for name in ("starting_quote_balance", "starting_base_balance", "starting_base_cost_basis_quote",
                 "max_risk_per_trade", "max_daily_loss"):
        if account_values.get(name) is not None:
            account_values[name] = D(account_values[name])
    account = CryptoSpotAccountConfig(**account_values)
    fees = payload["fees"]
    fee_config = CryptoSpotFeeConfiguration(
        D(fees["maker_rate"]), D(fees["taker_rate"]), fees["fee_currency"], fees["source"],
        datetime.fromisoformat(fees["configured_at"]), fees["actual_account_rates"], fees["version"],
    )
    costs_raw = payload["costs"]
    spot_fee_model = fee_config.taker_model()
    costs = CryptoSpotCostAssumptions(
        spot_fee_model, D(costs_raw["slippage_bps"]), D(costs_raw["spread_bps"]),
        costs_raw["source"], costs_raw["inputs_verified"], costs_raw["version"],
    )
    spec = CryptoSpotExecutionSpec(
        instrument, account.account_currency, filters.base_asset_precision,
        fee_config.fee_currency, fee_config.version, filters.source_endpoint,
        filters.retrieved_at_utc, filters,
    )
    candles = []
    for raw in payload["candles"]:
        candles.append(SimpleNamespace(
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            end_timestamp=datetime.fromisoformat(raw["end_timestamp"]),
            instrument_id=raw["instrument_id"], interval_minutes=int(raw["interval_minutes"]),
            open=D(raw["open"]), high=D(raw["high"]), low=D(raw["low"]), close=D(raw["close"]),
            volume=D(raw["volume"]), is_valid=True, source_ids=tuple(raw["source_ids"]),
        ))
    candidates = []
    for raw in payload["candidates"]:
        raw = dict(raw)
        for name in ("decision_timestamp", "reference_candle_timestamp", "expiry_timestamp"):
            raw[name] = datetime.fromisoformat(raw[name])
        for name in ("entry_trigger_price", "stop_price", "invalidation_price", "target_price",
                     "expected_reward_to_risk"):
            raw[name] = D(raw[name])
        raw["market_regime"] = MarketRegime(raw["market_regime"])
        raw["status"] = CandidateStatus(raw["status"])
        raw["indicator_snapshot"] = tuple(tuple(pair) for pair in raw["indicator_snapshot"])
        candidates.append(TradeCandidate(**raw))
    candidates.sort(key=lambda candidate: (candidate.decision_timestamp, candidate.candidate_id))
    if any(candidate.instrument_id != instrument.instrument_id for candidate in candidates):
        raise ValueError("candidate instrument does not match the configured crypto instrument")
    if any(candidate.asset_class != "crypto_spot" or candidate.direction != "LONG" for candidate in candidates):
        raise ValueError("crypto worker only accepts long-only crypto spot candidates")

    instrument_id = InstrumentId.from_str(instrument.instrument_id)
    venue = Venue(instrument_id.venue.value)
    base_currency = Currency.from_str(instrument.base_currency)
    quote_currency = Currency.from_str(instrument.quote_currency)
    # NautilusTrader 1.231.0 requires size_precision == size_increment precision.
    # The Binance LOT_SIZE step is 1e-5, so quantities and unused bar-volume
    # liquidity fields are projected to five decimals inside the simulator.
    qty_precision = max(0, -instrument.quantity_increment.normalize().as_tuple().exponent)
    size_increment = Quantity.from_str(f"{instrument.quantity_increment:.{qty_precision}f}")
    minimum_quantity = Quantity.from_str(f"{filters.lot_min_quantity:.{qty_precision}f}")
    maximum_quantity = filters.market_max_quantity.quantize(
        instrument.quantity_increment, rounding=ROUND_DOWN
    )
    venue_instrument = CurrencyPair(
        instrument_id, Symbol(instrument.symbol), base_currency, quote_currency,
        instrument.price_precision, qty_precision,
        Price.from_str(str(instrument.tick_size)),
        size_increment, 0, 0,
        min_quantity=minimum_quantity,
        max_quantity=Quantity.from_str(f"{maximum_quantity:.{qty_precision}f}"),
        min_notional=Money(filters.min_notional, quote_currency),
        max_notional=Money(filters.max_notional, quote_currency) if filters.max_notional else None,
        min_price=Price.from_str(f"{filters.min_price:.{instrument.price_precision}f}"),
        max_price=Price.from_str(f"{filters.max_price:.{instrument.price_precision}f}"),
        maker_fee=fee_config.maker_rate, taker_fee=fee_config.taker_rate,
    )
    interval = candles[0].interval_minutes
    bar_type = BarType(instrument_id, BarSpecification(interval, BarAggregation.MINUTE, PriceType.LAST), AggregationSource.EXTERNAL)
    def ns(timestamp):
        return int(timestamp.timestamp() * 1_000_000_000)
    bars = [Bar(
        bar_type,
        Price.from_str(f"{item.open:.{instrument.price_precision}f}"),
        Price.from_str(f"{item.high:.{instrument.price_precision}f}"),
        Price.from_str(f"{item.low:.{instrument.price_precision}f}"),
        Price.from_str(f"{item.close:.{instrument.price_precision}f}"),
        Quantity.from_str(
            f"{item.volume.quantize(D(1).scaleb(-qty_precision), rounding=ROUND_DOWN):.{qty_precision}f}"
        ),
        ns(item.end_timestamp), ns(item.end_timestamp),
    ) for item in candles]
    candidates_at = {}
    for candidate in candidates:
        candidates_at.setdefault(candidate.decision_timestamp, []).append(candidate)
    candle_by_end = {item.end_timestamp: item for item in candles}
    filter_reference_by_end = {}
    if filters.percent_price_avg_minutes is not None:
        price_window = deque(maxlen=filters.percent_price_avg_minutes)
        for item in candles:
            price_window.append(item.close)
            if len(price_window) == filters.percent_price_avg_minutes:
                filter_reference_by_end[item.end_timestamp] = sum(price_window, D(0)) / D(len(price_window))
    events, fills, rejections = [], [], []
    execution_references = {}
    ledger = CryptoSpotAccountLedger(spec, account)
    nautilus_fee_model = MakerTakerFeeModel()

    class CryptoCostFillModel(FillModel):
        """Apply configured costs to Nautilus's current execution-event price."""

        def __init__(self):
            # Do not activate the separate probabilistic one-tick slippage path.
            super().__init__(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=0)
            self.cost_model = CryptoExecutionCostModel(costs.spread_bps, costs.slippage_bps)

        def get_orderbook_for_fill_simulation(self, fill_instrument, order, best_bid, best_ask):
            reference = best_ask if order.is_buy else best_bid
            if reference is None:
                reference = best_bid if order.is_buy else best_ask
            if reference is None:
                raise RuntimeError("Nautilus supplied no execution-event reference price")
            side = "BUY" if order.is_buy else "SELL"
            adjusted = self.cost_model.adjusted_price(
                reference.as_decimal(), side, fill_instrument.price_increment.as_decimal()
            )
            execution_references[str(order.client_order_id)] = {
                "reference_price": str(reference.as_decimal()),
                "adjusted_price": str(adjusted),
                "cost_model_version": self.cost_model.version,
            }
            book = OrderBook(fill_instrument.id, BookType.L2_MBP)
            liquidity_side = OrderSide.SELL if order.is_buy else OrderSide.BUY
            book.add(
                BookOrder(
                    liquidity_side,
                    Price.from_str(f"{adjusted:.{fill_instrument.price_precision}f}"),
                    Quantity.from_str(str(order.quantity)),
                    1,
                ),
                0,
            )
            return book

    engine = BacktestEngine(BacktestEngineConfig(run_analysis=False))
    initial_quote = account.starting_quote_balance
    initial_base = account.starting_base_balance
    active = {"candidate": None, "order": None, "quantity": D(0), "approved_quantity": D(0), "entry_price": D(0),
              "entry_time": None, "exit_order": None, "entry_order": None,
              "entry_submitted": False, "protective_orders": []}
    processed = set()
    trades_in_period = {}
    realized_in_period = {}
    context = {}

    class CryptoStrategy(Strategy):
        def on_start(self):
            self.subscribe_bars(bar_type)

        def _emit(self, kind, ts, candidate_id=None, order_id=None, **extra):
            events.append({"type": kind, "timestamp": datetime.fromtimestamp(ts / 1e9, UTC).isoformat(),
                           "candidate_id": candidate_id, "order_id": order_id, **extra})

        def on_order_submitted(self, event):
            item = context.get(str(event.client_order_id), {})
            self._emit("order_submitted", event.ts_event, item.get("candidate_id"), str(event.client_order_id), role=item.get("role"))

        def on_order_accepted(self, event):
            item = context.get(str(event.client_order_id), {})
            self._emit("order_accepted", event.ts_event, item.get("candidate_id"), str(event.client_order_id), role=item.get("role"))

        def on_order_rejected(self, event):
            item = context.get(str(event.client_order_id), {})
            rejections.append({"candidate_id": item.get("candidate_id"), "reason": str(event.reason)})
            if item.get("role") == "entry":
                active["entry_order"] = None
                active["candidate"] = None
                active["entry_submitted"] = False
            self._emit("order_rejected", event.ts_event, item.get("candidate_id"), str(event.client_order_id), reason=str(event.reason))

        def on_order_expired(self, event):
            item = context.get(str(event.client_order_id), {})
            if item.get("role") == "entry":
                active["entry_order"] = None
                active["candidate"] = None
                active["entry_submitted"] = False
            self._emit("order_expired", event.ts_event, item.get("candidate_id"), str(event.client_order_id))

        def on_order_canceled(self, event):
            item = context.get(str(event.client_order_id), {})
            if item.get("role") == "entry" and active["quantity"] == 0:
                active["entry_order"] = None
                active["candidate"] = None
                active["entry_submitted"] = False
            self._emit("order_canceled", event.ts_event, item.get("candidate_id"), str(event.client_order_id),
                       role=item.get("role"))

        def on_order_filled(self, event):
            item = context.get(str(event.client_order_id), {})
            candidate_id = item.get("candidate_id")
            side = "BUY" if event.is_buy else "SELL"
            fee_currency = str(event.commission.currency)
            liquidity_class = getattr(event.liquidity_side, "name", str(event.liquidity_side)).upper()
            fill = ledger.project_nautilus_fill(
                str(event.client_order_id), side, event.last_qty.as_decimal(),
                event.last_px.as_decimal(), event.commission.as_decimal(), fee_currency,
                timestamp=datetime.fromtimestamp(event.ts_event / 1e9, UTC),
            )
            fill_record = {"order_id": fill.order_id, "candidate_id": candidate_id, "role": item.get("role"),
                           "side": side, "timestamp": fill.timestamp.isoformat(), "quantity": str(fill.quantity),
                           "price": str(fill.price), "fee": str(fill.fee_amount), "fee_currency": fee_currency,
                           "liquidity_side": liquidity_class,
                           "fee_classification": liquidity_class,
                           **execution_references.get(str(event.client_order_id), {}),
                           "realized_pnl_quote": str(fill.realized_pnl_quote)}
            fills.append(fill_record)
            self._emit("fill", event.ts_event, candidate_id, str(event.client_order_id), side=side,
                       quantity=str(fill.quantity), price=str(fill.price), fee=str(fill.fee_amount), fee_currency=fee_currency)
            if item.get("role") == "entry":
                active["quantity"] += fill.quantity
                active["entry_price"] = fill.price
                active["entry_time"] = fill.timestamp
                entry_order = self.cache.order(event.client_order_id)
                if entry_order is not None and entry_order.is_open:
                    self.cancel_order(entry_order)
                if entry_order is None or not entry_order.is_open:
                    active["entry_order"] = None
                candidate = active["candidate"]
                stop_order = self.order_factory.stop_market(
                    instrument_id=instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=event.last_qty,
                    trigger_price=Price.from_str(
                        f"{candidate.stop_price:.{instrument.price_precision}f}"
                    ),
                    trigger_type=TriggerType.LAST_PRICE,
                    time_in_force=TimeInForce.GTD,
                    expire_time=candidate.expiry_timestamp,
                    reduce_only=True,
                    tags=["simulation-only", "protective-stop", candidate.candidate_id],
                )
                context[str(stop_order.client_order_id)] = {
                    "role": "protective_stop", "candidate_id": candidate.candidate_id,
                }
                active["protective_orders"].append(stop_order)
                self.submit_order(stop_order)
            elif item.get("role") in ("exit", "protective_stop"):
                active["quantity"] -= fill.quantity
                if item.get("role") == "exit":
                    active["exit_order"] = None
                if active["quantity"] <= 0:
                    candidate = active["candidate"]
                    for stop_order in tuple(active["protective_orders"]):
                        if stop_order.is_open and str(stop_order.client_order_id) != str(event.client_order_id):
                            self.cancel_order(stop_order)
                    key = account.risk_period_key(fill.timestamp)
                    realized_in_period[key] = realized_in_period.get(key, D(0)) + fill.realized_pnl_quote
                    self._emit("position_closed", event.ts_event, candidate.candidate_id,
                               realized_pnl_quote=str(fill.realized_pnl_quote))
                    active.update({"candidate": None, "quantity": D(0), "approved_quantity": D(0), "entry_price": D(0),
                                   "entry_time": None, "exit_order": None, "entry_order": None,
                                   "entry_submitted": False, "protective_orders": []})

        def on_bar(self, bar):
            timestamp = datetime.fromtimestamp(bar.ts_event / 1e9, UTC)
            candle = candle_by_end.get(timestamp)
            if candle is None:
                return
            # Decisions are approved on completed decision candles, but orders are
            # submitted no earlier than the next event to prevent same-candle fills.
            pending = active.get("candidate")
            if (
                pending is not None
                and active.get("entry_order") is None
                and not active["entry_submitted"]
                and active["quantity"] == 0
                and self.portfolio.is_flat(instrument_id)
                and timestamp > pending.decision_timestamp
            ):
                if timestamp > pending.expiry_timestamp:
                    self._emit("candidate_expired_before_submission", bar.ts_event, pending.candidate_id)
                    active["candidate"] = None
                else:
                    order = self.order_factory.stop_market(
                        instrument_id=instrument_id, order_side=OrderSide.BUY,
                        quantity=Quantity.from_str(f"{active['approved_quantity']:.{qty_precision}f}"),
                        trigger_price=Price.from_str(f"{pending.entry_trigger_price:.{instrument.price_precision}f}"),
                        trigger_type=TriggerType.LAST_PRICE, time_in_force=TimeInForce.GTD,
                        expire_time=pending.expiry_timestamp,
                        tags=["simulation-only", pending.candidate_id, pending.strategy_id],
                    )
                    context[str(order.client_order_id)] = {"role": "entry", "candidate_id": pending.candidate_id}
                    active["entry_order"] = order
                    active["entry_submitted"] = True
                    self.submit_order(order)
                    self._emit("order_submit_intent", bar.ts_event, pending.candidate_id,
                               str(order.client_order_id), decision_timestamp=pending.decision_timestamp.isoformat())
            # For an open BTC position, stop wins when an OHLC candle touches both
            # stop and target; exits are market orders and fill on a later event.
            candidate = active.get("candidate")
            if candidate is not None and active["quantity"] > 0 and active.get("exit_order") is None:
                if candle.low <= candidate.stop_price:
                    reason = "stop_touched_stop_first"
                elif candle.high >= candidate.target_price:
                    reason = "target_touched"
                elif timestamp >= candidate.expiry_timestamp:
                    reason = "candidate_expiry"
                else:
                    reason = None
                if reason:
                    for stop_order in tuple(active["protective_orders"]):
                        if stop_order.is_open:
                            self.cancel_order(stop_order)
                    order = self.order_factory.market(
                        instrument_id=instrument_id, order_side=OrderSide.SELL,
                        quantity=Quantity.from_str(f"{active['quantity']:.{qty_precision}f}"),
                        time_in_force=TimeInForce.IOC, reduce_only=True,
                        tags=["simulation-only", candidate.candidate_id, reason],
                    )
                    context[str(order.client_order_id)] = {"role": "exit", "candidate_id": candidate.candidate_id,
                                                           "reason": reason}
                    active["exit_order"] = order
                    self.submit_order(order)
                    self._emit("exit_submit_intent", bar.ts_event, candidate.candidate_id,
                               str(order.client_order_id), reason=reason)
            for candidate in candidates_at.get(timestamp, ()):
                if candidate.candidate_id in processed:
                    self._emit("duplicate_candidate_rejected", bar.ts_event, candidate.candidate_id)
                    continue
                processed.add(candidate.candidate_id)
                if active.get("candidate") is not None or active["quantity"] > 0 or not self.portfolio.is_flat(instrument_id):
                    rejections.append({"candidate_id": candidate.candidate_id, "reason": "position_or_entry_pending"})
                    self._emit("risk_rejected", bar.ts_event, candidate.candidate_id, reason="position_or_entry_pending")
                    continue
                quote_free = self.portfolio.account(venue).balance_free(quote_currency)
                if quote_free is None:
                    rejections.append({"candidate_id": candidate.candidate_id, "reason": "quote_balance_unavailable"})
                    self._emit("risk_rejected", bar.ts_event, candidate.candidate_id,
                               reason="quote_balance_unavailable")
                    continue
                period = account.risk_period_key(timestamp)
                mark = D(str(bar.close.as_decimal()))
                unrealized = ledger.unrealized_pnl_quote(mark)
                period_pnl = realized_in_period.get(period, D(0))
                filter_reference = filter_reference_by_end.get(timestamp)
                decision = approve_crypto_candidate(
                    candidate, spec, account, available_quote_balance=quote_free.as_decimal(),
                    fee_model=spot_fee_model, costs=costs,
                    market_data=CryptoMarketDataState(timestamp, True, "completed Nautilus OHLCV bar"),
                    now=timestamp, realized_pnl_this_period=period_pnl,
                    unrealized_pnl_this_period=unrealized,
                    open_positions=0 if self.portfolio.is_flat(instrument_id) else 1,
                    trades_this_period=trades_in_period.get(period, 0),
                    filter_reference_price=filter_reference,
                )
                if not decision.approved:
                    rejections.append({"candidate_id": candidate.candidate_id, "reason": decision.reason})
                    self._emit("risk_rejected", bar.ts_event, candidate.candidate_id, reason=decision.reason)
                    continue
                active["candidate"] = candidate
                active["approved_quantity"] = decision.quantity
                trades_in_period[period] = trades_in_period.get(period, 0) + 1
                self._emit("risk_approved", bar.ts_event, candidate.candidate_id,
                           quantity=str(decision.quantity), modeled_loss_quote=str(decision.modeled_loss_quote),
                           cost_status=decision.cost_status,
                           cost_breakdown={key: str(value) for key, value in asdict(decision.cost_breakdown).items()})

    engine.add_venue(
        venue, OmsType.NETTING, AccountType.CASH,
        [Money(initial_quote, quote_currency), Money(initial_base, base_currency)],
        base_currency=None, fill_model=CryptoCostFillModel(),
        fee_model=nautilus_fee_model, liquidity_consumption=False, bar_execution=True,
        bar_adaptive_high_low_ordering=False, allow_cash_borrowing=False,
        reject_stop_orders=False,
    )
    engine.add_instrument(venue_instrument)
    engine.add_data(bars)
    engine.add_strategy(CryptoStrategy())
    try:
        engine.run()
        cash_account = engine.portfolio.account(venue)
        total = {str(currency): str(money.as_decimal()) for currency, money in cash_account.balances_total().items()}
        free = {str(currency): str(money.as_decimal()) for currency, money in cash_account.balances_free().items()}
        locked = {str(currency): str(money.as_decimal()) for currency, money in cash_account.balances_locked().items()}
        position_quantity = Decimal(str(engine.portfolio.net_position(instrument_id)))
        final_mark = Price.from_str(f"{candles[-1].close:.{instrument.price_precision}f}")
        nautilus_realized = engine.portfolio.realized_pnl(
            instrument_id, target_currency=quote_currency
        )
        nautilus_unrealized = engine.portfolio.unrealized_pnl(
            instrument_id, final_mark, target_currency=quote_currency
        )
    finally:
        engine.dispose()
    ledger.project_nautilus_reservations({
        instrument.quote_currency: D(locked.get(instrument.quote_currency, "0")),
        instrument.base_currency: D(locked.get(instrument.base_currency, "0")),
    })
    projected = ledger.snapshot(mark_price=candles[-1].close)
    projected_total = projected["balances"]
    errors = []
    pnl_tolerance = D("0.0000001")
    for currency in (instrument.base_currency, instrument.quote_currency):
        if D(total.get(currency, "0")) != D(projected_total[currency]):
            errors.append(f"{currency} total balance mismatch")
        if D(projected["reserved_balances"][currency]) != D(locked.get(currency, "0")):
            errors.append(f"{currency} reservation projection differs from Nautilus locked balance")
        if D(free.get(currency, "0")) + D(locked.get(currency, "0")) != D(total.get(currency, "0")):
            errors.append(f"{currency} free plus locked balance differs from Nautilus total")
        if D(projected["available_balances"][currency]) != D(free.get(currency, "0")):
            errors.append(f"{currency} available balance differs from Nautilus free balance")
    buy_quantity = sum((D(item["quantity"]) for item in fills if item["side"] == "BUY"), D(0))
    sell_quantity = sum((D(item["quantity"]) for item in fills if item["side"] == "SELL"), D(0))
    if D(str(position_quantity)) != buy_quantity - sell_quantity:
        errors.append("Nautilus BTC position differs from its fill quantity projection")
    fee_totals = {}
    for item in fills:
        fee_totals[item["fee_currency"]] = fee_totals.get(item["fee_currency"], D(0)) + D(item["fee"])
    for currency in (instrument.base_currency, instrument.quote_currency):
        if D(projected["fees_paid"][currency]) != fee_totals.get(currency, D(0)):
            errors.append(f"{currency} fee projection differs from Nautilus fill commissions")
    average_fill_prices = {}
    for side in ("BUY", "SELL"):
        selected = [item for item in fills if item["side"] == side]
        nautilus_average = (
            sum((D(item["price"]) * D(item["quantity"]) for item in selected), D(0))
            / sum((D(item["quantity"]) for item in selected), D(0))
            if selected else None
        )
        projected_selected = [item for item in ledger.fills if item.side == side]
        projected_average = (
            sum((item.price * item.quantity for item in projected_selected), D(0))
            / sum((item.quantity for item in projected_selected), D(0))
            if projected_selected else None
        )
        if nautilus_average != projected_average:
            errors.append(f"{side} average fill price differs from the fill projection")
        if nautilus_average is not None:
            average_fill_prices[side] = str(nautilus_average)
    event_realized = sum((D(item["realized_pnl_quote"]) for item in fills), D(0))
    if event_realized != D(projected["realized_pnl_quote"]):
        errors.append("realized P&L differs from Nautilus fill-event projection")
    projected_total_pnl = D(projected["realized_pnl_quote"]) + D(
        projected["unrealized_pnl_quote"]
    )
    nautilus_account_pnl = (
        D(total.get(instrument.quote_currency, "0"))
        - initial_quote
        + D(total.get(instrument.base_currency, "0")) * D(str(candles[-1].close))
        - (account.starting_base_cost_basis_quote or D(0))
    )
    if abs(nautilus_account_pnl - projected_total_pnl) > pnl_tolerance:
        errors.append(
            "account equity P&L differs from the crypto projection: "
            f"{nautilus_account_pnl} != {projected_total_pnl}"
        )
    if nautilus_unrealized is None:
        errors.append("Nautilus unrealized P&L is unavailable")
    elif abs(D(str(nautilus_unrealized.as_decimal())) - D(projected["unrealized_pnl_quote"])) > pnl_tolerance:
        errors.append(
            "unrealized P&L differs from Nautilus portfolio: "
            f"{nautilus_unrealized.as_decimal()} != {projected['unrealized_pnl_quote']}"
        )
    if (
        account.starting_base_balance == 0
        and D(str(position_quantity)) != D(projected_total[instrument.base_currency])
    ):
        errors.append("BTC position and projected BTC balance mismatch")
    if errors:
        raise RuntimeError("crypto ledger reconciliation failed: " + "; ".join(errors))
    return {
        "run_id": payload["run_id"], "nautilus_version": actual_version,
        "instrument_id": instrument.instrument_id, "account_type": "CASH",
        "account_currency": account.account_currency,
        "long_only": True,
        "leverage_enabled": False,
        "borrowing_enabled": False,
        "short_selling_enabled": False,
        "events": events, "fills": fills,
        "rejections": rejections, "nautilus_balances_total": total,
        "nautilus_balances_free": free, "nautilus_balances_locked": locked,
        "projected_account": projected,
        "reconciliation": {
            "passed": True,
            "errors": [],
            "nautilus_balances_total": total,
            "nautilus_balances_free": free,
            "nautilus_balances_locked": locked,
            "projected_balances": projected_total,
            "projected_available_balances": projected["available_balances"],
            "nautilus_net_btc_fills": str(buy_quantity - sell_quantity),
            "nautilus_position_btc": str(position_quantity),
            "fee_totals_by_currency": {key: str(value) for key, value in sorted(fee_totals.items())},
            "realized_pnl_quote": projected["realized_pnl_quote"],
            "nautilus_realized_pnl_quote": (
                str(nautilus_realized.as_decimal()) if nautilus_realized is not None else None
            ),
            "nautilus_account_pnl_quote": str(nautilus_account_pnl),
            "account_pnl_delta_quote": str(nautilus_account_pnl - projected_total_pnl),
            "unrealized_pnl_quote_at_final_bar": projected["unrealized_pnl_quote"],
            "nautilus_unrealized_pnl_quote_at_final_bar": (
                str(nautilus_unrealized.as_decimal())
                if nautilus_unrealized is not None else None
            ),
            "unrealized_pnl_delta_quote": (
                str(D(str(nautilus_unrealized.as_decimal())) - D(projected["unrealized_pnl_quote"]))
                if nautilus_unrealized is not None else None
            ),
            "pnl_tolerance_quote": str(pnl_tolerance),
            "average_fill_price_by_side": average_fill_prices,
        },
        "open_position_base_quantity": str(position_quantity),
        "starting_balances": {instrument.quote_currency: str(initial_quote), instrument.base_currency: str(initial_base)},
        "input_market_data_checksum": payload["input_market_data_checksum"],
    }


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] != "--worker":
        raise SystemExit("internal crypto Nautilus worker only")
    payload = json.load(sys.stdin)
    result = _worker(payload)
    print("CRYPTO_RESULT=" + json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
