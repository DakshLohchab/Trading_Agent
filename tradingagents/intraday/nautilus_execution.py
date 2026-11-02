"""Process-isolated NautilusTrader 1.231.0 historical execution adapter.

Call ``run_nautilus_execution`` from the TradingAgents ``.venv``. The adapter
marshals existing candles/candidates to the isolated ``.venv-nautilus`` worker;
strategy and candidate logic remain in the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import types
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.intraday.candidates import TradeCandidate
    from tradingagents.intraday.execution import EstimatedAdValoremCosts, RiskLimits
    from tradingagents.intraday.models import AggregatedCandle, InstrumentMetadata

SUPPORTED_VERSION = "1.231.0"
FILL_ASSUMPTIONS = (
    "Submitted inside a completed-bar callback; first matching opportunity is the next market-data event.",
    "NautilusTrader OHLCV bar execution with one deterministic adverse tick of slippage per fill.",
    "No bid/ask spread is fabricated; spread and date-specific exchange fee schedule remain incomplete.",
    "Target touch submits an IOC market exit for the next eligible event; a high touch alone is not treated as a limit fill.",
    "A resting protective stop-market is the only intrabar exit order; this makes stop win when stop and target levels share one OHLC candle.",
    "Stop-market gaps use NautilusTrader's next available bar execution price and can exceed the modeled risk budget.",
    "Bar liquidity consumption is enabled; partial fills are recorded and the unfilled entry remainder is canceled.",
    "Equity positions are forced out before the provided exchange session close; missing close bars leave the run marked incomplete.",
)
_ROOT = Path(__file__).resolve().parents[2]
_NAUTILUS_PYTHON = _ROOT / ".venv-nautilus" / "bin" / "python"


def run_nautilus_execution(
    instrument: InstrumentMetadata,
    candles: tuple[AggregatedCandle, ...] | list[AggregatedCandle],
    candidates: tuple[TradeCandidate, ...] | list[TradeCandidate],
    *,
    risk_limits: RiskLimits,
    costs: EstimatedAdValoremCosts,
    session_closes: dict[datetime, datetime],
    run_id: str,
    estimated_slippage_per_unit: Decimal,
    ledger_path: str | Path,
) -> dict:
    """Run an actual NautilusTrader simulation in its separately pinned venv."""
    if not _NAUTILUS_PYTHON.is_file():
        raise RuntimeError(f"NautilusTrader environment not found at {_NAUTILUS_PYTHON}")
    if not candles:
        raise ValueError("historical simulation requires candles")
    if not run_id.strip():
        raise ValueError("run_id is required")
    if not str(ledger_path).strip():
        raise ValueError("an execution ledger path is required")
    if any(not bar.is_valid for bar in candles):
        raise ValueError("invalid or incomplete candles cannot enter execution simulation")
    timestamps = [bar.timestamp for bar in candles]
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise ValueError("simulation candles must be unique and chronological")
    if any(bar.instrument_id != instrument.instrument_id for bar in candles):
        raise ValueError("simulation candles must match the selected instrument")
    intervals = {bar.interval_minutes for bar in candles}
    if len(intervals) != 1 or any(
        bar.end_timestamp - bar.timestamp != timedelta(minutes=bar.interval_minutes)
        for bar in candles
    ):
        raise ValueError("simulation requires complete, same-interval candles")
    if {bar.end_timestamp for bar in candles} - set(session_closes):
        raise ValueError("every candle completion must have its exchange session close")
    if any(session_closes[bar.end_timestamp] < bar.end_timestamp for bar in candles):
        raise ValueError("session close cannot precede a candle completion")
    by_timestamp = {bar.timestamp: bar for bar in candles}
    for previous, current in zip(candles, candles[1:], strict=False):
        if (
            session_closes[previous.end_timestamp] == session_closes[current.end_timestamp]
            and current.timestamp != previous.end_timestamp
        ):
            raise ValueError("missing or overlapping candles within an exchange session")
    for candidate in candidates:
        reference = by_timestamp.get(candidate.reference_candle_timestamp)
        if (
            candidate.instrument_id != instrument.instrument_id
            or reference is None
            or candidate.decision_timestamp != reference.end_timestamp
        ):
            raise ValueError(
                "candidate must reference its completed decision candle and instrument"
            )
    payload = {
        "nautilus_version": SUPPORTED_VERSION,
        "run_id": run_id,
        "instrument": _instrument_to_json(instrument),
        "candles": [
            {
                "timestamp": bar.timestamp.isoformat(),
                "end_timestamp": bar.end_timestamp.isoformat(),
                "instrument_id": bar.instrument_id,
                "interval_minutes": bar.interval_minutes,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "source_ids": bar.source_ids,
            }
            for bar in candles
        ],
        "candidates": [_candidate_to_json(candidate) for candidate in candidates],
        "risk_limits": {key: str(value) for key, value in asdict(risk_limits).items()},
        "costs": {key: str(value) for key, value in asdict(costs).items()},
        "session_closes": {
            key.astimezone(UTC).isoformat(): value.astimezone(UTC).isoformat()
            for key, value in session_closes.items()
        },
        "estimated_slippage_per_unit": str(estimated_slippage_per_unit),
    }
    payload["input_market_data_checksum"] = hashlib.sha256(
        json.dumps(payload["candles"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT)
    result = subprocess.run(
        [str(_NAUTILUS_PYTHON), str(Path(__file__).resolve()), "--worker"],
        input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=False,
        cwd=_ROOT,
        env=env,
    )
    if result.returncode:
        raise RuntimeError(
            f"NautilusTrader worker failed ({result.returncode}): {result.stderr[-4000:]}"
        )
    response = next(
        (
            line.removeprefix("NAUTILUS_RESULT=")
            for line in reversed(result.stdout.splitlines())
            if line.startswith("NAUTILUS_RESULT=")
        ),
        None,
    )
    if response is None:
        raise RuntimeError(f"NautilusTrader worker returned no result: {result.stderr[-2000:]}")
    output = json.loads(response)
    from tradingagents.intraday.execution import ExecutionLedger
    from tradingagents.intraday.metrics import ClosedTrade, EquityMark, calculate_metrics

    closed_trades = tuple(
        ClosedTrade(
            item["strategy_id"],
            datetime.fromisoformat(item["entry_timestamp"]),
            datetime.fromisoformat(item["exit_timestamp"]),
            Decimal(item["entry_notional"]),
            Decimal(item["exit_notional"]),
            Decimal(item["gross_pnl"]),
            Decimal(item["fees"]),
            Decimal(item["slippage"]),
        )
        for item in output["closed_trades"]
    )
    marks = tuple(
        EquityMark(
            datetime.fromisoformat(item["timestamp"]),
            Decimal(item["equity"]),
            Decimal(item["cash"]),
            Decimal(item["market_exposure"]),
            item["is_session_close"],
        )
        for item in output["equity_marks"]
    )
    strategy_ids = sorted({candidate.strategy_id for candidate in candidates})

    def metric_record(selected_trades):
        return asdict(
            calculate_metrics(
                starting_capital=Decimal(output["starting_capital"]),
                trades=selected_trades,
                equity_marks=marks,
                rejected_candidates=output["rejected_candidates"],
                rejected_orders=output["rejected_orders"],
                risk_limit_violations=output["risk_limit_violations"],
            )
        )

    def trade_totals(selected):
        selected = tuple(selected)
        net_values = [Decimal(item["net_pnl"]) for item in selected]
        winners = [value for value in net_values if value > 0]
        losers = [value for value in net_values if value < 0]
        gross_losers = -sum(losers, Decimal(0))
        return {
            "trade_count": len(selected),
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": str(Decimal(len(winners)) / len(selected)) if selected else None,
            "gross_pnl": str(sum((Decimal(item["gross_pnl"]) for item in selected), Decimal(0))),
            "net_pnl": str(sum(net_values, Decimal(0))),
            "fees": str(sum((Decimal(item["fees"]) for item in selected), Decimal(0))),
            "slippage": str(sum((Decimal(item["slippage"]) for item in selected), Decimal(0))),
            "average_win": str(sum(winners, Decimal(0)) / len(winners)) if winners else None,
            "average_loss": str(sum(losers, Decimal(0)) / len(losers)) if losers else None,
            "profit_factor": (
                str(sum(winners, Decimal(0)) / gross_losers) if gross_losers else None
            ),
            "expectancy_per_trade": str(sum(net_values, Decimal(0)) / len(selected))
            if selected
            else None,
        }

    output["performance"] = {
        "combined": metric_record(closed_trades),
        "by_strategy": (
            {
                strategy_id: metric_record(
                    tuple(trade for trade in closed_trades if trade.strategy_id == strategy_id)
                )
                for strategy_id in strategy_ids
            }
            if len(strategy_ids) <= 1
            else {}
        ),
        "by_strategy_trade_totals": {
            strategy_id: trade_totals(
                item for item in output["closed_trades"] if item["strategy_id"] == strategy_id
            )
            for strategy_id in strategy_ids
        },
        "by_market_regime_trade_totals": {
            regime: trade_totals(
                item for item in output["closed_trades"] if item["market_regime"] == regime
            )
            for regime in sorted({item["market_regime"] for item in output["closed_trades"]})
        },
        "by_entry_hour_local_trade_totals": {
            hour: trade_totals(
                item for item in output["closed_trades"] if item["entry_hour_local"] == hour
            )
            for hour in sorted({item["entry_hour_local"] for item in output["closed_trades"]})
        },
    }

    ledger = ExecutionLedger(ledger_path)
    checksum = output["input_market_data_checksum"]
    ledger_rows = []
    for candidate in candidates:
        ledger_rows.append(
            (
                candidate.decision_timestamp,
                "trade_candidate",
                candidate.candidate_id,
                None,
                {
                    **_candidate_to_json(candidate),
                    "input_market_data_checksum": checksum,
                    "risk_configuration_hash": output["risk_configuration_hash"],
                    "fill_model_version": output["fill_model_version"],
                    "cost_model_version": output["fee_model_version"],
                },
            )
        )
    for event in output["events"]:
        ledger_rows.append(
            (
                datetime.fromisoformat(event["timestamp"]),
                event["event_type"],
                event.get("candidate_id"),
                event.get("order_id"),
                {
                    key: value
                    for key, value in event.items()
                    if key not in ("event_type", "timestamp", "candidate_id", "order_id")
                },
            )
        )
    for mark in output["equity_marks"]:
        ledger_rows.append(
            (datetime.fromisoformat(mark["timestamp"]), "portfolio_snapshot", None, None, mark)
        )
    for trade in output["closed_trades"]:
        ledger_rows.append(
            (
                datetime.fromisoformat(trade["exit_timestamp"]),
                "trade_closed",
                trade["candidate_id"],
                None,
                trade,
            )
        )
    completed_at = datetime.fromisoformat(output["equity_marks"][-1]["timestamp"])
    ledger_rows.append(
        (
            completed_at,
            "run_completed",
            None,
            None,
            {
                key: value
                for key, value in output.items()
                if key not in ("events", "equity_marks", "fills")
            },
        )
    )
    for timestamp, event_type, candidate_id, order_id, payload_item in sorted(
        ledger_rows, key=lambda row: row[0]
    ):
        ledger.append(
            run_id=run_id,
            event_type=event_type,
            timestamp=timestamp,
            candidate_id=candidate_id,
            order_id=order_id,
            payload=payload_item,
        )
    output["execution_ledger_path"] = str(ledger_path)
    return output


def run_baseline_strategy_experiments(
    instrument: InstrumentMetadata,
    candles: tuple[AggregatedCandle, ...] | list[AggregatedCandle],
    candidates_by_strategy: dict[str, tuple[TradeCandidate, ...] | list[TradeCandidate]],
    *,
    risk_limits: RiskLimits,
    costs: EstimatedAdValoremCosts,
    session_closes: dict[datetime, datetime],
    ledger_directory: str | Path,
    run_id: str,
    estimated_slippage_per_unit: Decimal,
) -> dict[str, dict]:
    """Run the frozen Phase 2B VWAP, ORB, then shared-portfolio baseline."""
    required = ("vwap_trend_pullback", "opening_range_breakout")
    if set(candidates_by_strategy) != set(required):
        raise ValueError("baseline requires exactly the VWAP Pullback and ORB candidate sets")
    ledger_directory = Path(ledger_directory)
    outputs = {}
    for strategy_id in required:
        outputs[strategy_id] = run_nautilus_execution(
            instrument,
            candles,
            candidates_by_strategy[strategy_id],
            risk_limits=risk_limits,
            costs=costs,
            session_closes=session_closes,
            run_id=f"{run_id}-{strategy_id}",
            estimated_slippage_per_unit=estimated_slippage_per_unit,
            ledger_path=ledger_directory / f"{run_id}-{strategy_id}.sqlite",
        )
    combined = sorted(
        list(candidates_by_strategy[required[0]]) + list(candidates_by_strategy[required[1]]),
        key=lambda candidate: (candidate.decision_timestamp, candidate.candidate_id),
    )
    outputs["combined"] = run_nautilus_execution(
        instrument,
        candles,
        combined,
        risk_limits=risk_limits,
        costs=costs,
        session_closes=session_closes,
        run_id=f"{run_id}-combined",
        estimated_slippage_per_unit=estimated_slippage_per_unit,
        ledger_path=ledger_directory / f"{run_id}-combined.sqlite",
    )
    return outputs


def _instrument_to_json(instrument: InstrumentMetadata) -> dict:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(instrument).items()
    }


def _candidate_to_json(candidate: TradeCandidate) -> dict:
    value = asdict(candidate)
    for key, item in tuple(value.items()):
        if isinstance(item, (datetime, Decimal)):
            value[key] = item.isoformat() if isinstance(item, datetime) else str(item)
    value["market_regime"] = candidate.market_regime.value
    value["status"] = candidate.status.value
    value["indicator_snapshot"] = list(candidate.indicator_snapshot)
    return value


def _run_worker(payload: dict) -> dict:
    # The project root initializer imports TradingAgents' ordinary .venv-only
    # dependencies. This worker deliberately loads only the isolated intraday
    # modules and does not import or mutate the research application.
    root = Path(__file__).resolve().parents[2]
    package = types.ModuleType("tradingagents")
    package.__path__ = [str(root / "tradingagents")]
    package.__package__ = "tradingagents"
    sys.modules.setdefault("tradingagents", package)
    subpackage = types.ModuleType("tradingagents.intraday")
    subpackage.__path__ = [str(root / "tradingagents" / "intraday")]
    subpackage.__package__ = "tradingagents.intraday"
    sys.modules.setdefault("tradingagents.intraday", subpackage)
    from decimal import Decimal as D

    from nautilus_trader import __version__ as nautilus_version
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
    from nautilus_trader.model.currencies import Currency
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AccountType,
        AggregationSource,
        BarAggregation,
        OmsType,
        OrderSide,
        PriceType,
        TimeInForce,
        TriggerType,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    from tradingagents.intraday.candidates import CandidateStatus, TradeCandidate
    from tradingagents.intraday.execution import (
        EstimatedAdValoremCosts,
        RiskContext,
        RiskLimits,
        approve_candidate,
    )
    from tradingagents.intraday.models import InstrumentMetadata

    if nautilus_version != SUPPORTED_VERSION or payload["nautilus_version"] != SUPPORTED_VERSION:
        raise RuntimeError(
            f"worker requires NautilusTrader {SUPPORTED_VERSION}; found {nautilus_version}"
        )
    instrument = InstrumentMetadata(
        **{
            key: (
                D(value)
                if key
                in ("tick_size", "quantity_increment", "min_order_quantity", "min_order_notional")
                and value is not None
                else value
            )
            for key, value in payload["instrument"].items()
        }
    )
    if instrument.asset_class != "equity" or instrument.quote_currency != "INR":
        raise ValueError("initial Nautilus portfolio simulation supports INR cash equities only")
    candidates = []
    for raw in payload["candidates"]:
        raw = dict(raw)
        for key in ("decision_timestamp", "reference_candle_timestamp", "expiry_timestamp"):
            raw[key] = datetime.fromisoformat(raw[key])
        for key in (
            "entry_trigger_price",
            "stop_price",
            "invalidation_price",
            "target_price",
            "expected_reward_to_risk",
        ):
            raw[key] = D(raw[key])
        raw["market_regime"] = __import__(
            "tradingagents.intraday.regimes", fromlist=["MarketRegime"]
        ).MarketRegime(raw["market_regime"])
        raw["status"] = CandidateStatus(raw["status"])
        raw["indicator_snapshot"] = tuple(tuple(item) for item in raw["indicator_snapshot"])
        candidates.append(TradeCandidate(**raw))
    candidates.sort(key=lambda candidate: (candidate.decision_timestamp, candidate.candidate_id))
    candles = payload["candles"]
    candle_end_to_close = {
        datetime.fromisoformat(end): datetime.fromisoformat(close)
        for end, close in payload["session_closes"].items()
    }
    venue_code = InstrumentId.from_str(instrument.instrument_id).venue.value
    instrument_id = InstrumentId.from_str(instrument.instrument_id)
    currency = Currency.from_str("INR")
    interval = int(candles[0]["interval_minutes"])
    bar_type = BarType(
        instrument_id,
        BarSpecification(interval, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )

    def ns(value):
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)

    bars = [
        Bar(
            bar_type,
            Price.from_str(f"{D(item['open']):.{instrument.price_precision}f}"),
            Price.from_str(f"{D(item['high']):.{instrument.price_precision}f}"),
            Price.from_str(f"{D(item['low']):.{instrument.price_precision}f}"),
            Price.from_str(f"{D(item['close']):.{instrument.price_precision}f}"),
            Quantity.from_str(str(item["volume"])),
            ns(item["end_timestamp"]),
            ns(item["end_timestamp"]),
        )
        for item in candles
    ]
    raw_costs = payload["costs"]
    costs = EstimatedAdValoremCosts(
        **{key: D(value) if key.endswith("rate") else value for key, value in raw_costs.items()}
    )
    raw_limits = payload["risk_limits"]
    limits = RiskLimits(
        starting_cash=D(raw_limits["starting_cash"]),
        account_currency=raw_limits["account_currency"],
        max_risk_per_trade=D(raw_limits["max_risk_per_trade"]),
        max_daily_loss=D(raw_limits["max_daily_loss"]),
        max_concurrent_positions=int(raw_limits["max_concurrent_positions"]),
        max_new_trades_per_session=int(raw_limits["max_new_trades_per_session"]),
        include_unrealized_daily_loss=raw_limits["include_unrealized_daily_loss"] == "True",
    )
    instrument_model = Equity(
        instrument_id=instrument_id,
        raw_symbol=Symbol(instrument.symbol),
        currency=currency,
        price_precision=instrument.price_precision,
        price_increment=Price.from_str(str(instrument.tick_size)),
        lot_size=Quantity.from_str(str(instrument.quantity_increment)),
        ts_event=0,
        ts_init=0,
        min_quantity=(
            Quantity.from_str(str(instrument.min_order_quantity))
            if instrument.min_order_quantity
            else None
        ),
        taker_fee=max(
            (costs.brokerage_rate + costs.exchange_rate + costs.regulatory_rate)
            * (D(1) + costs.gst_rate)
            + costs.stamp_duty_buy_rate
            + costs.transaction_tax_buy_rate,
            (costs.brokerage_rate + costs.exchange_rate + costs.regulatory_rate)
            * (D(1) + costs.gst_rate)
            + costs.transaction_tax_sell_rate,
        ),
        maker_fee=max(
            (costs.brokerage_rate + costs.exchange_rate + costs.regulatory_rate)
            * (D(1) + costs.gst_rate)
            + costs.stamp_duty_buy_rate
            + costs.transaction_tax_buy_rate,
            (costs.brokerage_rate + costs.exchange_rate + costs.regulatory_rate)
            * (D(1) + costs.gst_rate)
            + costs.transaction_tax_sell_rate,
        ),
    )
    engine = BacktestEngine(BacktestEngineConfig(run_analysis=False))
    events: list[dict] = []
    fills: list[dict] = []
    closed_trades: list[dict] = []
    marks: list[dict] = []
    rejected_candidates = 0
    rejected_orders = 0
    risk_violations = 0
    order_context: dict[str, dict] = {}
    submitted_candidate_ids: set[str] = set()
    candidate_index = 0
    filled_entry: dict | None = None
    pending_entry_id: str | None = None
    protective_orders: list = []
    exit_pending = False
    realized_by_day: dict[str, D] = {}
    count_by_session: dict[str, int] = {}
    daily_limit_breach_days: set[str] = set()
    current_close = D(0)

    class CandidateStrategy(Strategy):
        def on_start(self):
            self.subscribe_bars(bar_type)

        def _event(self, event_type, timestamp, *, candidate_id=None, order_id=None, **data):
            events.append(
                {
                    "event_type": event_type,
                    "timestamp": datetime.fromtimestamp(timestamp / 1e9, UTC).isoformat(),
                    "candidate_id": candidate_id,
                    "order_id": order_id,
                    **data,
                }
            )

        def on_order_submitted(self, event):
            context = order_context.get(str(event.client_order_id), {})
            self._event(
                "order_submission",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
                role=context.get("role"),
            )

        def on_order_accepted(self, event):
            context = order_context.get(str(event.client_order_id), {})
            self._event(
                "order_acknowledgement",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
                role=context.get("role"),
            )

        def on_order_rejected(self, event):
            nonlocal rejected_orders, pending_entry_id
            rejected_orders += 1
            context = order_context.get(str(event.client_order_id), {})
            if context.get("role") == "entry":
                pending_entry_id = None
            self._event(
                "order_rejection",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
                reason=str(event.reason),
            )

        def on_order_canceled(self, event):
            nonlocal pending_entry_id
            context = order_context.get(str(event.client_order_id), {})
            if context.get("role") == "entry":
                pending_entry_id = None
            self._event(
                "order_cancellation",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
            )

        def on_order_expired(self, event):
            nonlocal pending_entry_id
            context = order_context.get(str(event.client_order_id), {})
            if context.get("role") == "entry":
                pending_entry_id = None
            self._event(
                "order_expiry",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
            )

        def _submit_market_exit(self, timestamp, reason):
            nonlocal exit_pending
            if exit_pending or self.portfolio.is_flat(instrument_id):
                return
            for stop_order in tuple(protective_orders):
                if stop_order.is_open:
                    self.cancel_order(stop_order)
            qty = D(str(self.portfolio.net_position(instrument_id)))
            if qty <= 0:
                return
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.SELL,
                quantity=Quantity.from_str(str(qty)),
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
                tags=["simulation-only", reason],
            )
            order_context[str(order.client_order_id)] = {
                "role": "exit",
                "candidate_id": filled_entry["candidate_id"],
                "benchmark": str(
                    filled_entry["target"] if reason.startswith("target_touched") else current_close
                ),
            }
            exit_pending = True
            self.submit_order(order)
            self._event(
                "position_modification",
                timestamp,
                candidate_id=filled_entry["candidate_id"],
                modification=reason,
            )

        def on_order_filled(self, event):
            nonlocal \
                filled_entry, \
                exit_pending, \
                rejected_candidates, \
                pending_entry_id, \
                risk_violations
            context = order_context.get(str(event.client_order_id), {})
            role = context.get("role")
            fill_time = datetime.fromtimestamp(event.ts_event / 1e9, UTC)
            commission = event.commission.as_decimal()
            filled_order = self.cache.order(event.client_order_id)
            fill_event_type = (
                "partial_fill"
                if filled_order is not None and filled_order.is_open
                else "completed_fill"
            )
            fill = {
                "candidate_id": context.get("candidate_id"),
                "role": role,
                "timestamp": fill_time.isoformat(),
                "price": str(event.last_px.as_decimal()),
                "quantity": str(event.last_qty.as_decimal()),
                "commission": str(commission),
                "order_id": str(event.client_order_id),
            }
            fills.append(fill)
            self._event(
                fill_event_type,
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
                price=fill["price"],
                quantity=fill["quantity"],
                commission=fill["commission"],
            )
            cash_delta = (
                -(event.last_px.as_decimal() * event.last_qty.as_decimal() + commission)
                if event.order_side == OrderSide.BUY
                else event.last_px.as_decimal() * event.last_qty.as_decimal() - commission
            )
            self._event(
                "cash_movement",
                event.ts_event,
                candidate_id=context.get("candidate_id"),
                order_id=str(event.client_order_id),
                currency="INR",
                delta=str(cash_delta),
                fee=str(commission),
            )
            if commission:
                self._event(
                    "fee",
                    event.ts_event,
                    candidate_id=context.get("candidate_id"),
                    order_id=str(event.client_order_id),
                    currency="INR",
                    amount=str(commission),
                    fee_model_version=costs.version,
                )
            if role == "entry":
                pending_entry_id = None
                entry_order = self.cache.order(event.client_order_id)
                if entry_order is not None and entry_order.is_open:
                    self.cancel_order(entry_order)
                if filled_entry is None:
                    filled_entry = {
                        "candidate_id": context["candidate_id"],
                        "strategy_id": context["strategy_id"],
                        "entry_price": D(0),
                        "quantity": D(0),
                        "entry_time": fill_time.isoformat(),
                        "stop": D(context["stop"]),
                        "target": D(context["target"]),
                        "expiry": datetime.fromisoformat(context["expiry"]),
                        "session_key": context["session_key"],
                        "modeled_max_loss": D(context["modeled_max_loss"]),
                        "market_regime": context["market_regime"],
                        "entry_hour_local": context["entry_hour_local"],
                        "entry_commission": D(0),
                        "entry_reference": D(context["entry_trigger"]),
                        "reference_gross_pnl": D(0),
                        "total_fees": D(0),
                        "slippage_cost": D(0),
                        "entry_notional": D(0),
                        "exit_notional": D(0),
                        "exit_quantity": D(0),
                        "exit_time": None,
                    }
                    self._event(
                        "position_opening",
                        event.ts_event,
                        candidate_id=context["candidate_id"],
                        strategy_id=context["strategy_id"],
                    )
                previous_qty = filled_entry["quantity"]
                fill_qty = event.last_qty.as_decimal()
                filled_entry["entry_price"] = (
                    filled_entry["entry_price"] * previous_qty
                    + event.last_px.as_decimal() * fill_qty
                ) / (previous_qty + fill_qty)
                filled_entry["quantity"] += fill_qty
                filled_entry["entry_commission"] += commission
                filled_entry["total_fees"] += commission
                filled_entry["entry_notional"] += event.last_px.as_decimal() * fill_qty
                filled_entry["slippage_cost"] += (
                    max(event.last_px.as_decimal() - filled_entry["entry_reference"], D(0))
                    * fill_qty
                )
                stop_order = self.order_factory.stop_market(
                    instrument_id=instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=event.last_qty,
                    trigger_price=Price.from_str(str(context["stop"])),
                    trigger_type=TriggerType.LAST_PRICE,
                    reduce_only=True,
                    tags=["simulation-only", "protective-stop", context["candidate_id"]],
                )
                order_context[str(stop_order.client_order_id)] = {
                    "role": "protective_stop",
                    "candidate_id": context["candidate_id"],
                    "benchmark": str(context["stop"]),
                }
                protective_orders.append(stop_order)
                self.submit_order(stop_order)
            elif role in ("exit", "protective_stop"):
                if filled_entry is not None:
                    exit_price = event.last_px.as_decimal()
                    exit_quantity = event.last_qty.as_decimal()
                    exit_reference = D(context.get("benchmark", exit_price))
                    filled_entry["reference_gross_pnl"] += (
                        exit_reference - filled_entry["entry_reference"]
                    ) * exit_quantity
                    filled_entry["slippage_cost"] += (
                        max(exit_reference - exit_price, D(0)) * exit_quantity
                    )
                    filled_entry["total_fees"] += commission
                    filled_entry["exit_notional"] += exit_price * exit_quantity
                    filled_entry["exit_quantity"] += exit_quantity
                    filled_entry["exit_time"] = fill_time.isoformat()
                    realized = (
                        (exit_price - filled_entry["entry_price"]) * exit_quantity
                        - commission
                        - filled_entry["entry_commission"]
                        * event.last_qty.as_decimal()
                        / max(filled_entry["quantity"], D(1))
                    )
                    local_day = (
                        fill_time.astimezone(
                            __import__("zoneinfo").ZoneInfo(instrument.exchange_timezone)
                        )
                        .date()
                        .isoformat()
                    )
                    realized_by_day[local_day] = realized_by_day.get(local_day, D(0)) + realized
                    if (
                        realized_by_day[local_day] <= -limits.max_daily_loss
                        and local_day not in daily_limit_breach_days
                    ):
                        daily_limit_breach_days.add(local_day)
                        risk_violations += 1
                    self._event(
                        "position_closing",
                        event.ts_event,
                        candidate_id=filled_entry["candidate_id"],
                        realized_pnl=str(realized),
                    )
                    if self.portfolio.is_flat(instrument_id):
                        closed_trades.append(
                            {
                                "strategy_id": filled_entry["strategy_id"],
                                "market_regime": filled_entry["market_regime"],
                                "entry_hour_local": filled_entry["entry_hour_local"],
                                "candidate_id": filled_entry["candidate_id"],
                                "entry_timestamp": filled_entry["entry_time"],
                                "exit_timestamp": filled_entry["exit_time"],
                                "entry_notional": str(filled_entry["entry_notional"]),
                                "exit_notional": str(filled_entry["exit_notional"]),
                                "gross_pnl": str(filled_entry["reference_gross_pnl"]),
                                "fees": str(filled_entry["total_fees"]),
                                "slippage": str(filled_entry["slippage_cost"]),
                                "net_pnl": str(
                                    filled_entry["reference_gross_pnl"]
                                    - filled_entry["total_fees"]
                                    - filled_entry["slippage_cost"]
                                ),
                            }
                        )
                        closed_net = (
                            filled_entry["reference_gross_pnl"]
                            - filled_entry["total_fees"]
                            - filled_entry["slippage_cost"]
                        )
                        if -closed_net > filled_entry["modeled_max_loss"]:
                            risk_violations += 1
                        filled_entry = None
                        exit_pending = False
                        protective_orders.clear()
                    elif role == "exit":
                        exit_pending = False
            elif role == "entry_rejected":
                rejected_candidates += 1

        def on_bar(self, bar):
            nonlocal \
                candidate_index, \
                current_close, \
                rejected_candidates, \
                filled_entry, \
                pending_entry_id
            timestamp = bar.ts_event
            current_close = bar.close.as_decimal()
            time_utc = datetime.fromtimestamp(timestamp / 1e9, UTC)
            local_day = (
                time_utc.astimezone(__import__("zoneinfo").ZoneInfo(instrument.exchange_timezone))
                .date()
                .isoformat()
            )
            if filled_entry is not None:
                low = bar.low.as_decimal()
                high = bar.high.as_decimal()
                close_at = candle_end_to_close.get(time_utc)
                if (
                    realized_by_day.get(local_day, D(0))
                    + (current_close - filled_entry["entry_price"]) * filled_entry["quantity"]
                    - filled_entry["entry_commission"]
                    <= -limits.max_daily_loss
                ):
                    self._event(
                        "risk_limit_exit_trigger",
                        timestamp,
                        candidate_id=filled_entry["candidate_id"],
                        reason="daily_loss_limit",
                    )
                    if low > filled_entry["stop"]:
                        self._submit_market_exit(timestamp, "daily_loss_limit_exit")
                if low <= filled_entry["stop"]:
                    self._event(
                        "protective_exit_trigger",
                        timestamp,
                        candidate_id=filled_entry["candidate_id"],
                        reason="stop_first_ohlcv",
                    )
                    # Native stop market order is the only resting protective order.
                elif high >= filled_entry["target"]:
                    self._submit_market_exit(timestamp, "target_touched_next_event_market_exit")
                elif time_utc >= filled_entry["expiry"]:
                    self._submit_market_exit(timestamp, "signal_time_expiry")
                elif close_at and time_utc >= close_at - __import__("datetime").timedelta(
                    minutes=interval * 2
                ):
                    self._submit_market_exit(timestamp, "same_session_forced_exit")
            while (
                candidate_index < len(candidates)
                and candidates[candidate_index].decision_timestamp < time_utc
            ):
                candidate = candidates[candidate_index]
                candidate_index += 1
                if candidate.candidate_id in submitted_candidate_ids:
                    rejected_candidates += 1
                    self._event(
                        "risk_rejection",
                        timestamp,
                        candidate_id=candidate.candidate_id,
                        reason="duplicate_candidate",
                    )
                    continue
                submitted_candidate_ids.add(candidate.candidate_id)
                if candidate.expiry_timestamp <= time_utc:
                    self._event("candidate_expired", timestamp, candidate_id=candidate.candidate_id)
                    continue
                account_obj = self.portfolio.account(Venue(venue_code))
                cash = account_obj.balance_free(currency).as_decimal()
                open_positions = 0 if self.portfolio.is_flat(instrument_id) else 1
                session_key = f"{instrument.venue}:{time_utc.astimezone(__import__('zoneinfo').ZoneInfo(instrument.exchange_timezone)).date().isoformat()}"
                context = RiskContext(
                    available_cash=cash,
                    realized_pnl_today=realized_by_day.get(local_day, D(0)),
                    unrealized_pnl_today=(
                        (current_close - filled_entry["entry_price"]) * filled_entry["quantity"]
                        if filled_entry
                        else D(0)
                    ),
                    open_positions=open_positions,
                    trades_this_session=count_by_session.get(session_key, 0),
                )
                decision = approve_candidate(
                    candidate,
                    instrument,
                    context,
                    limits,
                    costs,
                    estimated_slippage_per_unit=D(payload["estimated_slippage_per_unit"]),
                )
                if not decision.approved:
                    rejected_candidates += 1
                    self._event(
                        "risk_rejection",
                        timestamp,
                        candidate_id=candidate.candidate_id,
                        reason=decision.reason,
                        risk_config_hash=decision.risk_config_hash,
                    )
                    continue
                if pending_entry_id is not None:
                    rejected_candidates += 1
                    self._event(
                        "risk_rejection",
                        timestamp,
                        candidate_id=candidate.candidate_id,
                        reason="entry_order_pending",
                    )
                    continue
                if filled_entry is not None or not self.portfolio.is_flat(instrument_id):
                    rejected_candidates += 1
                    self._event(
                        "risk_rejection",
                        timestamp,
                        candidate_id=candidate.candidate_id,
                        reason="one_position_limit",
                    )
                    continue
                expiry = candidate.expiry_timestamp
                order = self.order_factory.stop_market(
                    instrument_id=instrument_id,
                    order_side=OrderSide.BUY,
                    quantity=Quantity.from_str(str(decision.quantity)),
                    trigger_price=Price.from_str(str(candidate.entry_trigger_price)),
                    trigger_type=TriggerType.LAST_PRICE,
                    time_in_force=TimeInForce.GTD,
                    expire_time=expiry,
                    tags=["simulation-only", candidate.candidate_id, candidate.strategy_id],
                )
                order_context[str(order.client_order_id)] = {
                    "role": "entry",
                    "candidate_id": candidate.candidate_id,
                    "strategy_id": candidate.strategy_id,
                    "entry_trigger": str(candidate.entry_trigger_price),
                    "market_regime": candidate.market_regime.value,
                    "entry_hour_local": candidate.decision_timestamp.astimezone(
                        __import__("zoneinfo").ZoneInfo(instrument.exchange_timezone)
                    ).hour,
                    "stop": str(candidate.stop_price),
                    "target": str(candidate.target_price),
                    "expiry": candidate.expiry_timestamp.isoformat(),
                    "session_key": decision.session_key,
                    "modeled_max_loss": str(decision.max_loss_at_stop),
                }
                pending_entry_id = str(order.client_order_id)
                self._event(
                    "risk_approval",
                    timestamp,
                    candidate_id=candidate.candidate_id,
                    quantity=str(decision.quantity),
                    max_loss_at_stop=str(decision.max_loss_at_stop),
                    risk_config_hash=decision.risk_config_hash,
                    session_key=decision.session_key,
                )
                self.submit_order(order)
                self._event(
                    "order_submission_intent",
                    timestamp,
                    candidate_id=candidate.candidate_id,
                    order_id=str(order.client_order_id),
                    decision_timestamp=candidate.decision_timestamp.isoformat(),
                )
                count_by_session[decision.session_key] = (
                    count_by_session.get(decision.session_key, 0) + 1
                )
            account_obj = self.portfolio.account(Venue(venue_code))
            cash = account_obj.balance_total(currency).as_decimal()
            position_qty = D(str(self.portfolio.net_position(instrument_id)))
            marks.append(
                {
                    "timestamp": time_utc.isoformat(),
                    "cash": str(cash),
                    "equity": str(cash + position_qty * current_close),
                    "market_exposure": str(position_qty * current_close),
                    "is_session_close": time_utc == candle_end_to_close.get(time_utc),
                }
            )

    maker_taker = MakerTakerFeeModel()
    engine.add_venue(
        Venue(venue_code),
        OmsType.NETTING,
        AccountType.CASH,
        [Money.from_decimal(limits.starting_cash, currency)],
        base_currency=currency,
        fill_model=FillModel(prob_fill_on_limit=1.0, prob_slippage=1.0, random_seed=0),
        fee_model=maker_taker,
        liquidity_consumption=True,
        bar_execution=True,
        bar_adaptive_high_low_ordering=False,
        allow_cash_borrowing=False,
        reject_stop_orders=False,
    )
    engine.add_instrument(instrument_model)
    engine.add_data(bars)
    engine.add_strategy(CandidateStrategy())
    try:
        engine.run()
    finally:
        engine.dispose()
    if filled_entry is not None:
        events.append(
            {
                "event_type": "unclosed_position_at_dataset_end",
                "candidate_id": filled_entry["candidate_id"],
                "timestamp": candles[-1]["end_timestamp"],
            }
        )
    return {
        "nautilus_version": nautilus_version,
        "run_id": payload["run_id"],
        "instrument_id": instrument.instrument_id,
        "account_currency": "INR",
        "starting_capital": str(limits.starting_cash),
        "events": events,
        "fills": fills,
        "closed_trades": closed_trades,
        "equity_marks": marks,
        "rejected_candidates": rejected_candidates,
        "rejected_orders": rejected_orders,
        "risk_limit_violations": risk_violations,
        "cost_completeness": "costs_incomplete_spread_and_historical_schedule_not_supplied",
        "fill_assumptions": FILL_ASSUMPTIONS,
        "fill_model_version": "nautilus-1.231.0-fillmodel-one-tick-slippage-v1",
        "fee_model_version": costs.version,
        "risk_configuration_hash": __import__(
            "tradingagents.intraday.execution", fromlist=["risk_configuration_hash"]
        ).risk_configuration_hash(limits, costs),
        "input_market_data_checksum": payload["input_market_data_checksum"],
        "open_position_at_end": filled_entry is not None,
    }


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] != "--worker":
        raise SystemExit("internal NautilusTrader worker entrypoint")
    payload = json.load(sys.stdin)
    result = _run_worker(payload)
    print("NAUTILUS_RESULT=" + json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
