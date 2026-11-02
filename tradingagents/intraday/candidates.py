"""Immutable research candidates and idempotent lifecycle tracking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from tradingagents.intraday.costs import CostEstimate
from tradingagents.intraday.models import InstrumentMetadata
from tradingagents.intraday.regimes import MarketRegime


class CandidateStatus(StrEnum):
    VALID = "valid_candidate"
    EXPIRED = "expired_candidate"


class SetupStatus(StrEnum):
    REJECTED = "rejected_setup"
    VALID = "valid_candidate"
    EXPIRED = "expired_candidate"
    INVALID_DATA = "invalid_data"
    DUPLICATE = "duplicate_candidate"


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    timestamp: datetime
    available_at: datetime
    bid: Decimal
    ask: Decimal
    source_id: str

    def __post_init__(self) -> None:
        for name in ("timestamp", "available_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"quote {name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        object.__setattr__(self, "bid", Decimal(str(self.bid)))
        object.__setattr__(self, "ask", Decimal(str(self.ask)))
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or self.bid <= 0
            or self.ask < self.bid
        ):
            raise ValueError("quote bid/ask are invalid")
        if self.timestamp > self.available_at:
            raise ValueError("quote timestamp cannot follow its availability timestamp")
        if not self.source_id.strip():
            raise ValueError("quote source_id cannot be blank")


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    candidate_id: str
    instrument_id: str
    venue: str
    asset_class: str
    strategy_id: str
    strategy_version: str
    direction: str
    decision_timestamp: datetime
    reference_candle_timestamp: datetime
    entry_trigger_type: str
    entry_trigger_price: Decimal
    stop_price: Decimal
    invalidation_price: Decimal
    target_price: Decimal
    expected_reward_to_risk: Decimal
    expiry_timestamp: datetime
    market_regime: MarketRegime
    indicator_snapshot: tuple[tuple[str, str | None], ...]
    data_quality_status: str
    data_source: str
    replay_id: str
    configuration_hash: str
    estimated_net_return: Decimal | None
    cost_status: str
    status: CandidateStatus = CandidateStatus.VALID

    def __post_init__(self) -> None:
        for name in ("decision_timestamp", "reference_candle_timestamp", "expiry_timestamp"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"candidate {name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        for name in (
            "entry_trigger_price",
            "stop_price",
            "invalidation_price",
            "target_price",
            "expected_reward_to_risk",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite():
                raise ValueError(f"candidate {name} must be finite")
            object.__setattr__(self, name, value)
        if (
            not self.candidate_id
            or not self.instrument_id
            or not self.strategy_id
            or not self.replay_id
        ):
            raise ValueError("candidate identity, instrument, strategy and replay are required")
        object.__setattr__(self, "indicator_snapshot", tuple(self.indicator_snapshot))
        if self.direction != "LONG":
            raise ValueError("Phase 2B candidates are long-only")
        if self.expiry_timestamp <= self.decision_timestamp:
            raise ValueError("candidate expiry must follow the decision timestamp")
        if not (self.stop_price < self.entry_trigger_price < self.target_price):
            raise ValueError("long candidate prices must satisfy stop < entry < target")
        if not self.stop_price <= self.invalidation_price < self.entry_trigger_price:
            raise ValueError("long candidate invalidation must be below entry and at/above stop")
        if self.expected_reward_to_risk <= 0:
            raise ValueError("expected reward-to-risk must be positive")


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    status: SetupStatus
    reason: str
    candidate: TradeCandidate | None = None


def hash_configuration(config: Any) -> str:
    def normalize(value: Any):
        if hasattr(value, "__dataclass_fields__"):
            return {key: normalize(getattr(value, key)) for key in value.__dataclass_fields__}
        if isinstance(value, dict):
            return {
                str(key): normalize(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, StrEnum):
            return value.value
        return value

    encoded = json.dumps(normalize(config), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def round_to_tick(price: Decimal, tick_size: Decimal, *, rounding: str) -> Decimal:
    mode = ROUND_CEILING if rounding == "up" else ROUND_FLOOR
    units = (price / tick_size).to_integral_value(rounding=mode)
    return units * tick_size


def make_candidate(
    *,
    instrument: InstrumentMetadata,
    strategy_id: str,
    strategy_version: str,
    decision_timestamp: datetime,
    reference_candle_timestamp: datetime,
    entry_trigger_type: str,
    entry_trigger_price: Decimal,
    stop_price: Decimal,
    invalidation_price: Decimal,
    target_price: Decimal,
    expiry_timestamp: datetime,
    market_regime: MarketRegime,
    indicator_snapshot: tuple[tuple[str, str | None], ...],
    data_quality_status: str,
    replay_id: str,
    configuration_hash: str,
    cost_estimate: CostEstimate,
) -> TradeCandidate:
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() is None
        for timestamp in (decision_timestamp, reference_candle_timestamp, expiry_timestamp)
    ):
        raise ValueError("candidate timestamps must be timezone-aware")
    decision_timestamp = decision_timestamp.astimezone(UTC)
    reference_candle_timestamp = reference_candle_timestamp.astimezone(UTC)
    expiry_timestamp = expiry_timestamp.astimezone(UTC)
    entry = round_to_tick(Decimal(str(entry_trigger_price)), instrument.tick_size, rounding="up")
    stop = round_to_tick(Decimal(str(stop_price)), instrument.tick_size, rounding="down")
    invalidation = round_to_tick(
        Decimal(str(invalidation_price)), instrument.tick_size, rounding="down"
    )
    target = round_to_tick(Decimal(str(target_price)), instrument.tick_size, rounding="up")
    reward_risk = (target - entry) / (entry - stop) if entry > stop else Decimal(0)
    identifier_content = (
        instrument.instrument_id,
        instrument.venue,
        instrument.asset_class,
        instrument.base_currency,
        instrument.quote_currency,
        instrument.price_precision,
        str(instrument.tick_size),
        str(instrument.quantity_increment),
        str(instrument.min_order_quantity),
        str(instrument.min_order_notional),
        instrument.exchange_timezone,
        instrument.session_calendar,
        instrument.data_source,
        instrument.provenance,
        strategy_id,
        strategy_version,
        decision_timestamp.isoformat(),
        reference_candle_timestamp.isoformat(),
        entry_trigger_type,
        str(entry),
        str(stop),
        str(invalidation),
        str(target),
        expiry_timestamp.isoformat(),
        replay_id,
        configuration_hash,
        indicator_snapshot,
    )
    candidate_id = hashlib.sha256(
        json.dumps(identifier_content, separators=(",", ":")).encode()
    ).hexdigest()
    return TradeCandidate(
        candidate_id,
        instrument.instrument_id,
        instrument.venue,
        instrument.asset_class,
        strategy_id,
        strategy_version,
        "LONG",
        decision_timestamp,
        reference_candle_timestamp,
        entry_trigger_type,
        entry,
        stop,
        invalidation,
        target,
        reward_risk,
        expiry_timestamp,
        market_regime,
        indicator_snapshot,
        data_quality_status,
        instrument.data_source,
        replay_id,
        configuration_hash,
        cost_estimate.estimated_net_return,
        "available" if cost_estimate.available else "unavailable",
    )


class CandidateRegistry:
    """Idempotent candidate emission; expiration is final and IDs stay reserved."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._current: dict[str, TradeCandidate] = {}
        self._scopes: dict[str, str] = {}

    def emit(
        self, candidate: TradeCandidate, *, now: datetime, scope: str | None = None
    ) -> StrategyEvaluation:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("registry time must be timezone-aware")
        now = now.astimezone(UTC)
        if candidate.candidate_id in self._seen:
            existing = self._current[candidate.candidate_id]
            if existing.status == CandidateStatus.EXPIRED:
                return StrategyEvaluation(SetupStatus.EXPIRED, "candidate_expired", existing)
            return StrategyEvaluation(SetupStatus.DUPLICATE, "candidate_already_emitted", existing)
        if scope is not None and scope in self._scopes:
            existing = self._current[self._scopes[scope]]
            return StrategyEvaluation(
                SetupStatus.DUPLICATE, "setup_scope_already_emitted", existing
            )
        self._seen.add(candidate.candidate_id)
        if scope is not None:
            self._scopes[scope] = candidate.candidate_id
        if now >= candidate.expiry_timestamp:
            expired = replace(candidate, status=CandidateStatus.EXPIRED)
            self._current[candidate.candidate_id] = expired
            return StrategyEvaluation(SetupStatus.EXPIRED, "candidate_expired_at_emission", expired)
        self._current[candidate.candidate_id] = candidate
        return StrategyEvaluation(SetupStatus.VALID, "candidate_created", candidate)

    def advance(self, now: datetime) -> tuple[TradeCandidate, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("registry time must be timezone-aware")
        now = now.astimezone(UTC)
        newly_expired = []
        for candidate_id, candidate in tuple(self._current.items()):
            if candidate.status == CandidateStatus.VALID and now >= candidate.expiry_timestamp:
                expired = replace(candidate, status=CandidateStatus.EXPIRED)
                self._current[candidate_id] = expired
                newly_expired.append(expired)
        return tuple(newly_expired)

    def get(self, candidate_id: str) -> TradeCandidate | None:
        return self._current.get(candidate_id)
