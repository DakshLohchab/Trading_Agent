"""Simulation risk, cost assumptions, and audit records for intraday backtests.

This module contains no broker connectivity. The NautilusTrader adapter consumes
its decisions and remains the sole owner of simulated orders and account state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from tradingagents.intraday.candidates import TradeCandidate
from tradingagents.intraday.models import InstrumentMetadata

D = Decimal


class TransactionCostModel(Protocol):
    version: str
    completeness: str
    configuration_snapshot: tuple[tuple[str, str], ...]

    def entry_cost_per_unit(self, price: Decimal) -> Decimal: ...

    def exit_cost_per_unit(self, price: Decimal) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class EstimatedAdValoremCosts:
    """Explicit estimated costs; callers must provide every rate (fractional)."""

    brokerage_rate: Decimal
    exchange_rate: Decimal
    regulatory_rate: Decimal
    transaction_tax_buy_rate: Decimal
    transaction_tax_sell_rate: Decimal
    stamp_duty_buy_rate: Decimal
    gst_rate: Decimal
    version: str = "estimated-ad-valorem-v1"
    completeness: str = "estimated_schedule_based"

    @property
    def configuration_snapshot(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (name, str(getattr(self, name)))
            for name in (
                "brokerage_rate",
                "exchange_rate",
                "regulatory_rate",
                "transaction_tax_buy_rate",
                "transaction_tax_sell_rate",
                "stamp_duty_buy_rate",
                "gst_rate",
                "version",
                "completeness",
            )
        )

    def __post_init__(self) -> None:
        for name in (
            "brokerage_rate",
            "exchange_rate",
            "regulatory_rate",
            "transaction_tax_buy_rate",
            "transaction_tax_sell_rate",
            "stamp_duty_buy_rate",
            "gst_rate",
        ):
            value = D(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative decimal")
            object.__setattr__(self, name, value)

    def entry_cost_per_unit(self, price: Decimal) -> Decimal:
        turnover = price
        taxable = turnover * (self.brokerage_rate + self.exchange_rate + self.regulatory_rate)
        gst = taxable * self.gst_rate
        return (
            taxable
            + gst
            + turnover * self.transaction_tax_buy_rate
            + turnover * self.stamp_duty_buy_rate
        )

    def exit_cost_per_unit(self, price: Decimal) -> Decimal:
        turnover = price
        taxable = turnover * (self.brokerage_rate + self.exchange_rate + self.regulatory_rate)
        gst = taxable * self.gst_rate
        return taxable + gst + turnover * self.transaction_tax_sell_rate


@dataclass(frozen=True, slots=True)
class RiskLimits:
    starting_cash: Decimal = D("10000")
    account_currency: str = "INR"
    max_risk_per_trade: Decimal = D("25")
    max_daily_loss: Decimal = D("75")
    max_concurrent_positions: int = 1
    max_new_trades_per_session: int = 3
    include_unrealized_daily_loss: bool = True

    def __post_init__(self) -> None:
        for name in ("starting_cash", "max_risk_per_trade", "max_daily_loss"):
            value = D(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if self.max_concurrent_positions <= 0 or self.max_new_trades_per_session <= 0:
            raise ValueError("position and daily trade limits must be positive")
        if self.account_currency != self.account_currency.upper():
            raise ValueError("account currency must be uppercase ISO code")


@dataclass(frozen=True, slots=True)
class RiskContext:
    available_cash: Decimal
    realized_pnl_today: Decimal = D(0)
    unrealized_pnl_today: Decimal = D(0)
    open_positions: int = 0
    trades_this_session: int = 0
    session_date: date | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    candidate_id: str
    quantity: Decimal | None
    max_loss_at_stop: Decimal | None
    estimated_entry_cost: Decimal | None
    estimated_exit_cost: Decimal | None
    session_key: str
    risk_config_hash: str


def risk_configuration_hash(limits: RiskLimits, costs: TransactionCostModel) -> str:
    values = asdict(limits)
    values["cost_model"] = {
        "version": costs.version,
        "completeness": costs.completeness,
        "configuration_snapshot": costs.configuration_snapshot,
    }
    encoded = json.dumps(values, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _floor_increment(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def approve_candidate(
    candidate: TradeCandidate,
    instrument: InstrumentMetadata,
    context: RiskContext,
    limits: RiskLimits,
    costs: TransactionCostModel,
    *,
    estimated_slippage_per_unit: Decimal,
) -> RiskDecision:
    """Risk-gate a candidate before an order exists; all amounts use account currency."""
    session_day = candidate.decision_timestamp.astimezone(
        ZoneInfo(instrument.exchange_timezone)
    ).date()
    session_key = f"{instrument.venue}:{session_day.isoformat()}"
    config_hash = risk_configuration_hash(limits, costs)

    def reject(reason: str) -> RiskDecision:
        return RiskDecision(
            False, reason, candidate.candidate_id, None, None, None, None, session_key, config_hash
        )

    if candidate.status.value != "valid_candidate":
        return reject("candidate_not_valid")
    if candidate.direction != "LONG" or instrument.asset_class != "equity":
        return reject("unsupported_direction_or_asset_class")
    if instrument.quote_currency != limits.account_currency:
        return reject("currency_conversion_unavailable")
    if context.open_positions >= limits.max_concurrent_positions:
        return reject("concurrent_position_limit")
    if context.trades_this_session >= limits.max_new_trades_per_session:
        return reject("session_trade_limit")
    daily_pnl = context.realized_pnl_today
    if limits.include_unrealized_daily_loss:
        daily_pnl += context.unrealized_pnl_today
    if daily_pnl <= -limits.max_daily_loss:
        return reject("daily_loss_limit_exhausted")
    if context.available_cash <= 0:
        return reject("insufficient_cash")

    slip = D(str(estimated_slippage_per_unit))
    if not slip.is_finite() or slip < 0:
        return reject("invalid_slippage_assumption")
    entry = candidate.entry_trigger_price + slip
    stop_fill = candidate.stop_price - slip
    if stop_fill <= 0 or stop_fill >= entry:
        return reject("invalid_conservative_stop")
    entry_cost = costs.entry_cost_per_unit(entry)
    exit_cost = costs.exit_cost_per_unit(stop_fill)
    loss_per_unit = entry - stop_fill + entry_cost + exit_cost
    if loss_per_unit <= 0:
        return reject("invalid_unit_risk")
    daily_remaining = limits.max_daily_loss + daily_pnl
    risk_budget = min(limits.max_risk_per_trade, daily_remaining)
    quantity = _floor_increment(risk_budget / loss_per_unit, instrument.quantity_increment)
    cash_per_unit = entry + entry_cost
    quantity = min(
        quantity,
        _floor_increment(context.available_cash / cash_per_unit, instrument.quantity_increment),
    )
    if instrument.min_order_quantity is not None and quantity < instrument.min_order_quantity:
        return reject("below_minimum_quantity")
    if quantity <= 0:
        return reject("risk_or_cash_budget_below_one_increment")
    notional = quantity * entry
    if instrument.min_order_notional is not None and notional < instrument.min_order_notional:
        return reject("below_minimum_notional")
    modeled_loss = quantity * loss_per_unit
    if modeled_loss > limits.max_risk_per_trade or modeled_loss > daily_remaining:
        return reject("modeled_loss_exceeds_limit")
    return RiskDecision(
        True,
        "approved",
        candidate.candidate_id,
        quantity,
        modeled_loss,
        quantity * entry_cost,
        quantity * exit_cost,
        session_key,
        config_hash,
    )


class ExecutionLedger:
    """Append-only SQLite ledger, intentionally separate from research memory."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS execution_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                "event_type TEXT NOT NULL, event_timestamp TEXT NOT NULL, "
                "candidate_id TEXT, order_id TEXT, payload_json TEXT NOT NULL)"
            )

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        timestamp: datetime,
        payload: dict,
        candidate_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("ledger event timestamps must be timezone-aware")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO execution_events "
                "(run_id,event_type,event_timestamp,candidate_id,order_id,payload_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    event_type,
                    timestamp.astimezone(UTC).isoformat(),
                    candidate_id,
                    order_id,
                    json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")),
                ),
            )

    def events(self, run_id: str) -> tuple[dict, ...]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT sequence,event_type,event_timestamp,candidate_id,order_id,payload_json "
                "FROM execution_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        import json

        return tuple(
            {
                "sequence": row[0],
                "event_type": row[1],
                "event_timestamp": row[2],
                "candidate_id": row[3],
                "order_id": row[4],
                "payload": json.loads(row[5]),
            }
            for row in rows
        )
