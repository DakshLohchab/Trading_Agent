"""Isolated, long-only crypto spot accounting and risk boundary.

This module is simulation-only. It has no network, broker, or exchange-order API.
All capital, risk, instrument filters, fee, spread, and slippage inputs are explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tradingagents.intraday.candidates import TradeCandidate
from tradingagents.intraday.crypto_exchange import BinanceSpotFilters
from tradingagents.intraday.models import InstrumentMetadata

D = Decimal
Side = Literal["BUY", "SELL"]


class CryptoRiskError(ValueError):
    """A crypto order or risk decision failed a fail-closed boundary check."""


@dataclass(frozen=True, slots=True)
class CryptoSpotExecutionSpec:
    instrument: InstrumentMetadata
    account_currency: str
    quantity_precision: int
    fee_currency: str
    fee_model_id: str
    specification_source: str
    retrieved_at: datetime
    exchange_filters: BinanceSpotFilters | None = None

    def __post_init__(self) -> None:
        if self.instrument.asset_class != "crypto_spot":
            raise CryptoRiskError("crypto execution spec requires CRYPTO_SPOT instrument metadata")
        if self.instrument.session_calendar != "24/7":
            raise CryptoRiskError("crypto spot instruments require the 24/7 calendar")
        if self.account_currency != self.instrument.quote_currency:
            raise CryptoRiskError("account currency must equal the spot quote currency; FX is unavailable")
        if self.fee_currency not in (self.instrument.base_currency, self.instrument.quote_currency):
            raise CryptoRiskError("fee currency must be the base or quote asset")
        if not self.fee_model_id.strip() or not self.specification_source.strip():
            raise CryptoRiskError("fee model ID and instrument specification source are required")
        if self.instrument.min_order_quantity is None or self.instrument.min_order_notional is None:
            raise CryptoRiskError("minimum quantity and notional metadata are required")
        if self.quantity_precision < 0:
            raise CryptoRiskError("quantity precision cannot be negative")
        if self.instrument.quantity_increment.normalize().as_tuple().exponent < -self.quantity_precision:
            raise CryptoRiskError("quantity step requires more precision than configured")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise CryptoRiskError("instrument specification retrieval time must include UTC offset")
        try:
            ZoneInfo(self.instrument.exchange_timezone)
        except ZoneInfoNotFoundError as exc:
            raise CryptoRiskError("instrument timezone is not available") from exc
        if self.exchange_filters is not None:
            filters = self.exchange_filters
            if (
                filters.symbol != self.instrument.symbol
                or filters.base_asset != self.instrument.base_currency
                or filters.quote_asset != self.instrument.quote_currency
                or filters.tick_size != self.instrument.tick_size
                or filters.lot_step_size != self.instrument.quantity_increment
                or filters.lot_min_quantity != self.instrument.min_order_quantity
                or filters.min_notional != self.instrument.min_order_notional
            ):
                raise CryptoRiskError("instrument metadata conflicts with the Binance filter snapshot")


@dataclass(frozen=True, slots=True)
class CryptoSpotFeeModel:
    """Explicit fee assumption; account-specific rates require sourced account data."""

    fee_rate: Decimal
    fee_currency: str
    source: str
    version: str
    account_specific: bool

    def __post_init__(self) -> None:
        rate = D(str(self.fee_rate))
        if not rate.is_finite() or rate < 0:
            raise CryptoRiskError("fee rate must be explicit, finite, and non-negative")
        object.__setattr__(self, "fee_rate", rate)
        if not self.fee_currency.strip() or not self.source.strip() or not self.version.strip():
            raise CryptoRiskError("fee currency, source, and model version are required")

    @property
    def cost_status(self) -> str:
        return "COST_COMPLETE" if self.account_specific else "COST_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class CryptoSpotFeeConfiguration:
    """Explicit maker/taker schedule; integration orders are charged at taker rate."""

    maker_rate: Decimal
    taker_rate: Decimal
    fee_currency: str
    source: str
    configured_at: datetime
    actual_account_rates: bool
    version: str

    def __post_init__(self) -> None:
        for name in ("maker_rate", "taker_rate"):
            value = D(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise CryptoRiskError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if not self.fee_currency.strip() or not self.source.strip() or not self.version.strip():
            raise CryptoRiskError("fee currency, source, and version are required")
        if self.configured_at.tzinfo is None or self.configured_at.utcoffset() is None:
            raise CryptoRiskError("fee configuration timestamp must include UTC offset")

    @property
    def cost_status(self) -> str:
        return "COST_COMPLETE" if self.actual_account_rates else "COST_INCOMPLETE"

    def taker_model(self) -> CryptoSpotFeeModel:
        return CryptoSpotFeeModel(
            self.taker_rate,
            self.fee_currency,
            self.source,
            self.version,
            self.actual_account_rates,
        )


@dataclass(frozen=True, slots=True)
class CryptoSpotCostAssumptions:
    fee_model: CryptoSpotFeeModel | None
    slippage_per_unit: Decimal | None
    spread_bps: Decimal | None
    source: str | None
    inputs_verified: bool
    version: str

    def __post_init__(self) -> None:
        if self.slippage_per_unit is not None:
            slip = D(str(self.slippage_per_unit))
            if not slip.is_finite() or slip < 0:
                raise CryptoRiskError("slippage per unit must be finite and non-negative")
            object.__setattr__(self, "slippage_per_unit", slip)
        if self.spread_bps is not None:
            spread = D(str(self.spread_bps))
            if not spread.is_finite() or spread < 0:
                raise CryptoRiskError("spread assumption must be finite and non-negative")
            object.__setattr__(self, "spread_bps", spread)
        if not self.version.strip():
            raise CryptoRiskError("cost assumption version is required")

    @property
    def available(self) -> bool:
        return self.fee_model is not None and self.slippage_per_unit is not None and self.spread_bps is not None

    @property
    def cost_status(self) -> str:
        if not self.available or not self.fee_model.account_specific or not self.source or not self.inputs_verified:
            return "COST_INCOMPLETE"
        return "COST_COMPLETE"


@dataclass(frozen=True, slots=True)
class CryptoSpotAccountConfig:
    """A separately named virtual quote-currency account; no borrowed balances."""

    account_id: str
    account_currency: str
    starting_quote_balance: Decimal
    starting_base_balance: Decimal
    starting_base_cost_basis_quote: Decimal | None
    max_risk_per_trade: Decimal
    max_daily_loss: Decimal
    max_open_positions: int
    max_trades_per_risk_period: int
    risk_period_minutes: int
    risk_period_timezone: str
    max_market_data_age_minutes: int

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.account_currency.strip():
            raise CryptoRiskError("crypto account ID and currency are required")
        for name in ("starting_quote_balance", "starting_base_balance", "max_risk_per_trade", "max_daily_loss"):
            value = D(str(getattr(self, name)))
            if not value.is_finite() or value < 0 or name in ("max_risk_per_trade", "max_daily_loss") and value == 0:
                raise CryptoRiskError(f"{name} must be explicit and non-negative")
            object.__setattr__(self, name, value)
        if self.starting_base_balance and self.starting_base_cost_basis_quote is None:
            raise CryptoRiskError("starting BTC inventory requires an explicit quote-currency cost basis")
        if self.starting_base_cost_basis_quote is not None:
            basis = D(str(self.starting_base_cost_basis_quote))
            if not basis.is_finite() or basis < 0:
                raise CryptoRiskError("starting base cost basis must be finite and non-negative")
            object.__setattr__(self, "starting_base_cost_basis_quote", basis)
        if min(self.max_open_positions, self.max_trades_per_risk_period, self.risk_period_minutes,
               self.max_market_data_age_minutes) <= 0:
            raise CryptoRiskError("position, trade, and risk-period limits must be positive")
        if not self.risk_period_timezone.strip():
            raise CryptoRiskError("risk-period timezone is required")
        if self.risk_period_minutes > 1440:
            raise CryptoRiskError("crypto risk periods cannot exceed one day")
        try:
            ZoneInfo(self.risk_period_timezone)
        except ZoneInfoNotFoundError as exc:
            raise CryptoRiskError("unknown crypto risk-period timezone") from exc

    def risk_period_key(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise CryptoRiskError("risk-period timestamp must be timezone-aware")
        local = timestamp.astimezone(ZoneInfo(self.risk_period_timezone))
        minute_of_day = local.hour * 60 + local.minute
        period_index = minute_of_day // self.risk_period_minutes
        return f"{local.date().isoformat()}:{period_index}"


@dataclass(frozen=True, slots=True)
class CryptoMarketDataState:
    latest_completed_candle_at: datetime
    integrity_ok: bool
    source: str

    def __post_init__(self) -> None:
        if self.latest_completed_candle_at.tzinfo is None or self.latest_completed_candle_at.utcoffset() is None:
            raise CryptoRiskError("latest completed candle timestamp must be timezone-aware")
        if not self.source.strip():
            raise CryptoRiskError("market data provenance is required")


@dataclass(frozen=True, slots=True)
class CryptoRiskDecision:
    approved: bool
    reason: str
    quantity: Decimal | None
    modeled_loss_quote: Decimal | None
    cost_status: str


@dataclass(frozen=True, slots=True)
class CryptoFill:
    order_id: str
    side: Side
    timestamp: datetime
    price: Decimal
    quantity: Decimal
    fee_amount: Decimal
    fee_currency: str
    realized_pnl_quote: Decimal


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def approve_crypto_candidate(
    candidate: TradeCandidate,
    spec: CryptoSpotExecutionSpec,
    account: CryptoSpotAccountConfig,
    *,
    available_quote_balance: Decimal,
    fee_model: CryptoSpotFeeModel | None,
    costs: CryptoSpotCostAssumptions | None,
    market_data: CryptoMarketDataState,
    now: datetime,
    realized_pnl_this_period: Decimal,
    unrealized_pnl_this_period: Decimal,
    open_positions: int,
    trades_this_period: int,
    filter_reference_price: Decimal | None = None,
) -> CryptoRiskDecision:
    """Size a cash-only long entry in the isolated quote-currency account."""
    incomplete = "COST_INCOMPLETE"

    def reject(reason: str) -> CryptoRiskDecision:
        return CryptoRiskDecision(False, reason, None, None, incomplete)

    instrument = spec.instrument
    if candidate.status.value != "valid_candidate" or candidate.direction != "LONG":
        return reject("candidate_not_valid_long")
    if candidate.instrument_id != instrument.instrument_id or candidate.asset_class != "crypto_spot":
        return reject("instrument_or_asset_class_mismatch")
    if account.account_currency != instrument.quote_currency or spec.account_currency != account.account_currency:
        return reject("account_currency_mismatch_fx_unavailable")
    if fee_model is None or costs is None or not costs.available or costs.fee_model != fee_model:
        return reject("fee_or_execution_cost_model_missing")
    if fee_model.fee_currency != spec.fee_currency:
        return reject("fee_currency_mismatch")
    if fee_model.version != spec.fee_model_id:
        return reject("fee_model_version_mismatch")
    if account.account_id == "equity-inr":
        return reject("crypto_account_must_be_isolated_from_equity_account")
    if open_positions >= account.max_open_positions:
        return reject("open_position_limit")
    if trades_this_period >= account.max_trades_per_risk_period:
        return reject("risk_period_trade_limit")
    period_pnl = D(str(realized_pnl_this_period)) + D(str(unrealized_pnl_this_period))
    if period_pnl <= -account.max_daily_loss:
        return reject("daily_loss_limit_exhausted")
    if not market_data.integrity_ok:
        return reject("market_data_integrity_failed")
    if now.tzinfo is None or now.utcoffset() is None:
        return reject("risk_time_must_be_timezone_aware")
    if now.astimezone(UTC) < candidate.decision_timestamp:
        return reject("candidate_not_yet_available")
    if market_data.latest_completed_candle_at > candidate.decision_timestamp:
        return reject("future_or_incomplete_candle")
    if candidate.decision_timestamp - market_data.latest_completed_candle_at > timedelta(
        minutes=account.max_market_data_age_minutes
    ):
        return reject("market_data_stale")
    fee_rate = fee_model.fee_rate
    if fee_model.fee_currency not in (instrument.base_currency, instrument.quote_currency):
        return reject("unsupported_fee_currency")
    tick = instrument.tick_size
    if candidate.entry_trigger_price % tick or candidate.stop_price % tick:
        return reject("price_tick_violation")
    slip = costs.slippage_per_unit
    spread_half = candidate.entry_trigger_price * costs.spread_bps / D(20000)
    entry = candidate.entry_trigger_price + slip + spread_half
    exit_price = candidate.stop_price - slip - candidate.stop_price * costs.spread_bps / D(20000)
    if entry <= 0 or exit_price <= 0 or exit_price >= entry:
        return reject("invalid_conservative_stop")
    entry_fee_per_unit = entry * fee_rate
    exit_fee_per_unit = exit_price * fee_rate
    loss_per_unit = entry - exit_price + entry_fee_per_unit + exit_fee_per_unit
    if loss_per_unit <= 0:
        return reject("invalid_unit_risk")
    risk_budget = min(account.max_risk_per_trade, account.max_daily_loss + period_pnl)
    quantity = _floor_step(risk_budget / loss_per_unit, instrument.quantity_increment)
    available = D(str(available_quote_balance))
    quote_per_unit = entry * (D(1) + fee_rate if fee_model.fee_currency == instrument.quote_currency else D(1))
    if available < 0 or quote_per_unit <= 0:
        return reject("invalid_quote_balance")
    quantity = min(quantity, _floor_step(available / quote_per_unit, instrument.quantity_increment))
    if quantity < instrument.min_order_quantity:
        return reject("below_minimum_quantity_or_available_cash")
    if quantity * entry < instrument.min_order_notional:
        return reject("below_minimum_notional")
    if spec.exchange_filters is not None:
        filters = spec.exchange_filters
        if quantity > filters.lot_max_quantity or quantity > filters.market_max_quantity:
            return reject("maximum_order_quantity_exceeded")
        if not filters.min_price <= entry <= filters.max_price:
            return reject("price_filter_violation")
        if filters.max_notional is not None and quantity * entry > filters.max_notional:
            return reject("maximum_notional_exceeded")
        if filters.percent_price_avg_minutes is not None:
            if filter_reference_price is None or D(str(filter_reference_price)) <= 0:
                return reject("percent_price_reference_unavailable")
            reference = D(str(filter_reference_price))
            if not reference * filters.bid_multiplier_down <= candidate.entry_trigger_price <= reference * filters.bid_multiplier_up:
                return reject("percent_price_by_side_filter_violation")
    modeled_loss = quantity * loss_per_unit
    if modeled_loss > account.max_risk_per_trade or modeled_loss > account.max_daily_loss + period_pnl:
        return reject("modeled_loss_exceeds_risk_limit")
    return CryptoRiskDecision(True, "approved", quantity, modeled_loss, costs.cost_status)


@dataclass(slots=True)
class _Reservation:
    side: Side
    remaining_quantity: Decimal
    submitted_at: datetime
    limit_price: Decimal
    fee_model: CryptoSpotFeeModel
    reserved_currency: str
    reserved_remaining: Decimal


class CryptoSpotAccountLedger:
    """Deterministic, append-only fill ledger with explicit BTC/USDT balances."""

    def __init__(
        self,
        spec: CryptoSpotExecutionSpec,
        account: CryptoSpotAccountConfig,
    ):
        if account.account_currency != spec.account_currency:
            raise CryptoRiskError("virtual crypto account and execution spec currencies differ")
        self.spec = spec
        self.account = account
        base, quote = spec.instrument.base_currency, spec.instrument.quote_currency
        self.balances = {quote: account.starting_quote_balance, base: account.starting_base_balance}
        self.reserved_balances = {quote: D(0), base: D(0)}
        self.fees_paid = {quote: D(0), base: D(0)}
        self.base_cost_basis_quote = account.starting_base_cost_basis_quote or D(0)
        self.realized_pnl_quote = D(0)
        self.fills: list[CryptoFill] = []
        self.events: list[dict] = []
        self._orders: dict[str, _Reservation] = {}

    def available_balance(self, currency: str) -> Decimal:
        if currency not in self.balances:
            return D(0)
        return self.balances[currency] - self.reserved_balances[currency]

    def reserve_order(
        self,
        order_id: str,
        side: Side,
        quantity: Decimal,
        limit_price: Decimal,
        *,
        submitted_at: datetime,
        fee_model: CryptoSpotFeeModel | None,
    ) -> None:
        if not order_id.strip() or order_id in self._orders:
            raise CryptoRiskError("order ID must be unique and non-empty")
        if fee_model is None:
            raise CryptoRiskError("fee model is required; crypto order rejected")
        if fee_model.fee_currency != self.spec.fee_currency:
            raise CryptoRiskError("fee model currency does not match instrument specification")
        if fee_model.version != self.spec.fee_model_id:
            raise CryptoRiskError("fee model version does not match instrument specification")
        if side not in ("BUY", "SELL"):
            raise CryptoRiskError("only spot BUY and SELL orders are supported")
        qty, price = D(str(quantity)), D(str(limit_price))
        if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
            raise CryptoRiskError("order submission time must be timezone-aware")
        if not qty.is_finite() or qty <= 0 or qty % self.spec.instrument.quantity_increment:
            raise CryptoRiskError("order quantity violates the configured quantity step")
        if qty < self.spec.instrument.min_order_quantity:
            raise CryptoRiskError("order quantity is below the configured minimum")
        if not price.is_finite() or price <= 0 or price % self.spec.instrument.tick_size:
            raise CryptoRiskError("order price violates the configured price tick")
        if qty * price < self.spec.instrument.min_order_notional:
            raise CryptoRiskError("order notional is below the configured minimum")
        base, quote = self.spec.instrument.base_currency, self.spec.instrument.quote_currency
        if side == "BUY":
            currency = quote
            amount = qty * price * (D(1) + fee_model.fee_rate if fee_model.fee_currency == quote else D(1))
        else:
            currency = base
            amount = qty * (D(1) + fee_model.fee_rate if fee_model.fee_currency == base else D(1))
        if amount > self.available_balance(currency):
            raise CryptoRiskError(f"insufficient available {currency}; borrowing is disabled")
        self.reserved_balances[currency] += amount
        self._orders[order_id] = _Reservation(side, qty, submitted_at.astimezone(UTC), price,
                                               fee_model, currency, amount)
        self.events.append({"type": "balance_reserved", "order_id": order_id,
                            "currency": currency, "amount": str(amount),
                            "timestamp": submitted_at.astimezone(UTC).isoformat()})

    def apply_fill(
        self,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        *,
        timestamp: datetime,
    ) -> CryptoFill:
        reservation = self._orders.get(order_id)
        if reservation is None:
            raise CryptoRiskError("fill has no matching reserved order")
        qty, fill_price = D(str(quantity)), D(str(price))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.astimezone(UTC) <= reservation.submitted_at:
            raise CryptoRiskError("fills must occur on a later market-data event")
        if not qty.is_finite() or qty <= 0 or qty > reservation.remaining_quantity:
            raise CryptoRiskError("fill quantity is invalid or exceeds the remaining order")
        instrument = self.spec.instrument
        if qty % instrument.quantity_increment:
            raise CryptoRiskError("fill quantity violates the configured quantity step")
        if not fill_price.is_finite() or fill_price <= 0 or fill_price % instrument.tick_size:
            raise CryptoRiskError("fill price violates the configured price tick")
        fee_model = reservation.fee_model
        fee_amount = (qty * fill_price if fee_model.fee_currency == instrument.quote_currency else qty) * fee_model.fee_rate
        base, quote = instrument.base_currency, instrument.quote_currency
        if reservation.side == "BUY":
            gross_quote = qty * fill_price
            quote_debit = gross_quote + (fee_amount if fee_model.fee_currency == quote else D(0))
            base_credit = qty - (fee_amount if fee_model.fee_currency == base else D(0))
            base_debit = D(0)
            quote_credit = D(0)
        else:
            base_debit = qty + (fee_amount if fee_model.fee_currency == base else D(0))
            quote_credit = qty * fill_price - (fee_amount if fee_model.fee_currency == quote else D(0))
            quote_debit = D(0)
            base_credit = D(0)
            if quote_credit < 0:
                raise CryptoRiskError("fee exceeds sell proceeds")
        release = reservation.reserved_remaining * qty / reservation.remaining_quantity
        free_after_release = self.available_balance(reservation.reserved_currency) + release
        actual_debit = quote_debit if reservation.reserved_currency == quote else base_debit
        if actual_debit > free_after_release:
            raise CryptoRiskError(f"fill would overdraw {reservation.reserved_currency}; borrowing is disabled")
        next_reserved = dict(self.reserved_balances)
        next_reserved[reservation.reserved_currency] -= release
        next_balances = dict(self.balances)
        next_balances[quote] = next_balances[quote] - quote_debit + quote_credit
        next_balances[base] = next_balances[base] - base_debit + base_credit
        if next_balances[quote] < next_reserved[quote] or next_balances[base] < next_reserved[base]:
            raise CryptoRiskError("fill would make a balance negative or consume reserved assets")
        self.reserved_balances = next_reserved
        self.balances = next_balances
        reservation.reserved_remaining -= release
        reservation.remaining_quantity -= qty
        if fee_amount:
            self.fees_paid[fee_model.fee_currency] += fee_amount
        if reservation.side == "BUY":
            self.base_cost_basis_quote += quote_debit
            realized = D(0)
        else:
            avg_cost = self.base_cost_basis_quote / (self.balances[base] + base_debit) if self.balances[base] + base_debit else D(0)
            cost_removed = avg_cost * base_debit
            realized = quote_credit - cost_removed
            self.base_cost_basis_quote -= cost_removed
            self.realized_pnl_quote += realized
        fill = CryptoFill(order_id, reservation.side, timestamp.astimezone(UTC), fill_price, qty,
                          fee_amount, fee_model.fee_currency, realized)
        self.fills.append(fill)
        self.events.append({"type": "fill", "order_id": order_id,
                            "side": reservation.side, "timestamp": fill.timestamp.isoformat(),
                            "price": str(fill_price), "quantity": str(qty),
                            "fee_amount": str(fee_amount), "fee_currency": fee_model.fee_currency,
                            "realized_pnl_quote": str(realized)})
        if reservation.remaining_quantity == 0:
            self._orders.pop(order_id)
        return fill

    def project_nautilus_fill(
        self,
        order_id: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        *,
        timestamp: datetime,
    ) -> CryptoFill:
        """Project an authoritative Nautilus fill without reserving/debiting orders.

        This method is the integration boundary: Nautilus owns orders and cash;
        this ledger is an independently derived accounting projection.
        """
        qty, px, fee = D(str(quantity)), D(str(price)), D(str(fee_amount))
        if side not in ("BUY", "SELL"):
            raise CryptoRiskError("projected fill side must be BUY or SELL")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise CryptoRiskError("projected fill timestamp must be timezone-aware")
        if not all(value.is_finite() for value in (qty, px, fee)) or qty <= 0 or px <= 0 or fee < 0:
            raise CryptoRiskError("projected fill values must be finite and valid")
        instrument = self.spec.instrument
        if qty % instrument.quantity_increment or px % instrument.tick_size:
            raise CryptoRiskError("projected fill violates instrument precision")
        if fee_currency not in (instrument.base_currency, instrument.quote_currency):
            raise CryptoRiskError("projected fee currency must be BTC or USDT")
        base, quote = instrument.base_currency, instrument.quote_currency
        next_balances = dict(self.balances)
        if side == "BUY":
            quote_debit = qty * px + (fee if fee_currency == quote else D(0))
            base_credit = qty - (fee if fee_currency == base else D(0))
            if next_balances[quote] < quote_debit or base_credit < 0:
                raise CryptoRiskError("Nautilus BUY fill would overdraw projected balances")
            next_balances[quote] -= quote_debit
            next_balances[base] += base_credit
            realized = D(0)
            next_basis = self.base_cost_basis_quote + quote_debit
        else:
            base_debit = qty + (fee if fee_currency == base else D(0))
            quote_credit = qty * px - (fee if fee_currency == quote else D(0))
            if next_balances[base] < base_debit or quote_credit < 0:
                raise CryptoRiskError("Nautilus SELL fill would overdraw projected balances")
            prior_qty = next_balances[base]
            avg_cost = self.base_cost_basis_quote / prior_qty if prior_qty else D(0)
            next_balances[base] -= base_debit
            next_balances[quote] += quote_credit
            realized = quote_credit - avg_cost * base_debit
            next_basis = max(D(0), self.base_cost_basis_quote - avg_cost * base_debit)
            self.realized_pnl_quote += realized
        self.balances = next_balances
        self.base_cost_basis_quote = next_basis
        self.fees_paid[fee_currency] += fee
        fill = CryptoFill(
            order_id, side, timestamp.astimezone(UTC), px, qty, fee, fee_currency, realized
        )
        self.fills.append(fill)
        self.events.append({
            "type": "nautilus_fill_projection", "order_id": order_id,
            "side": side, "timestamp": fill.timestamp.isoformat(),
            "price": str(px), "quantity": str(qty), "fee_amount": str(fee),
            "fee_currency": fee_currency, "realized_pnl_quote": str(realized),
        })
        return fill

    def project_nautilus_reservations(self, locked_balances: dict[str, Decimal]) -> None:
        """Mirror Nautilus CASH locks for reconciliation without creating reservations."""
        next_reserved = {currency: D(0) for currency in self.reserved_balances}
        for currency, amount in locked_balances.items():
            if currency not in next_reserved:
                if D(str(amount)) != 0:
                    raise CryptoRiskError("Nautilus locked an unsupported asset")
                continue
            value = D(str(amount))
            if not value.is_finite() or value < 0 or value > self.balances[currency]:
                raise CryptoRiskError("Nautilus locked balance is invalid")
            next_reserved[currency] = value
        self.reserved_balances = next_reserved
        self.events.append({
            "type": "nautilus_reservation_projection",
            "locked_balances": {key: str(value) for key, value in sorted(next_reserved.items())},
        })

    def cancel_order(self, order_id: str) -> None:
        reservation = self._orders.pop(order_id, None)
        if reservation is None:
            return
        self.reserved_balances[reservation.reserved_currency] -= reservation.reserved_remaining
        self.events.append({"type": "reservation_released", "order_id": order_id,
                            "currency": reservation.reserved_currency,
                            "amount": str(reservation.reserved_remaining)})

    def unrealized_pnl_quote(self, mark_price: Decimal) -> Decimal:
        mark = D(str(mark_price))
        if not mark.is_finite() or mark <= 0:
            raise CryptoRiskError("mark price must be finite and positive")
        return self.balances[self.spec.instrument.base_currency] * mark - self.base_cost_basis_quote

    def snapshot(self, *, mark_price: Decimal | None = None) -> dict:
        """Return explicit total/free/reserved balances and quote-currency P&L."""
        unrealized = self.unrealized_pnl_quote(mark_price) if mark_price is not None else None
        return {
            "account_id": self.account.account_id,
            "account_currency": self.account.account_currency,
            "balances": {currency: str(amount) for currency, amount in sorted(self.balances.items())},
            "reserved_balances": {currency: str(amount) for currency, amount in sorted(self.reserved_balances.items())},
            "available_balances": {currency: str(self.available_balance(currency)) for currency in sorted(self.balances)},
            "fees_paid": {currency: str(amount) for currency, amount in sorted(self.fees_paid.items())},
            "realized_pnl_quote": str(self.realized_pnl_quote),
            "unrealized_pnl_quote": str(unrealized) if unrealized is not None else None,
            "base_cost_basis_quote": str(self.base_cost_basis_quote),
            "fill_count": len(self.fills),
            "event_count": len(self.events),
        }
