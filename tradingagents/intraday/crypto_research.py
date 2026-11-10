"""Strict BTCUSDT research profile, cost scenarios, and canonical hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotFeeConfiguration,
)

D = Decimal


class FeeClassification(StrEnum):
    ACTUAL_ACCOUNT_RATE = "ACTUAL_ACCOUNT_RATE"
    RESEARCH_ASSUMPTION = "RESEARCH_ASSUMPTION"


class CostClassification(StrEnum):
    OBSERVED = "OBSERVED"
    RESEARCH_ASSUMPTION = "RESEARCH_ASSUMPTION"


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        result = D(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be a decimal string or null") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


@dataclass(frozen=True, slots=True)
class CryptoResearchProfile:
    account_id: str
    account_currency: str
    starting_capital: Decimal | None
    max_risk_per_trade: Decimal | None
    max_daily_loss: Decimal | None
    max_concurrent_positions: int | None
    max_trades_per_utc_day: int | None
    long_only: bool
    leverage_enabled: bool
    borrowing_enabled: bool
    short_selling_enabled: bool
    maker_fee_bps: Decimal | None
    taker_fee_bps: Decimal | None
    fee_currency: str | None
    fee_source: str | None
    fee_configured_at: datetime | None
    fee_classification: FeeClassification | None
    spread_bps: Decimal | None
    spread_source: str | None
    spread_classification: CostClassification | None
    slippage_bps: Decimal | None
    slippage_source: str | None
    slippage_classification: CostClassification | None
    profile_version: str = "btcusdt-research-profile-v1"

    def __post_init__(self) -> None:
        if not self.account_id.strip() or self.account_currency != "USDT":
            raise ValueError("research profile requires a distinct account ID and USDT currency")
        for name in (
            "starting_capital",
            "max_risk_per_trade",
            "max_daily_loss",
            "maker_fee_bps",
            "taker_fee_bps",
            "spread_bps",
            "slippage_bps",
        ):
            value = _optional_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        for name in ("max_concurrent_positions", "max_trades_per_utc_day"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or null")
        if self.starting_capital == 0:
            raise ValueError("starting capital must be positive when configured")
        if self.max_risk_per_trade == 0 or self.max_daily_loss == 0:
            raise ValueError("risk limits must be positive when configured")
        if self.fee_currency is not None and self.fee_currency != "USDT":
            raise ValueError("only USDT-denominated commissions are supported")
        if self.fee_configured_at is not None and (
            self.fee_configured_at.tzinfo is None or self.fee_configured_at.utcoffset() is None
        ):
            raise ValueError("fee timestamp must include a UTC offset")
        if self.long_only is not True or any(
            (self.leverage_enabled, self.borrowing_enabled, self.short_selling_enabled)
        ):
            raise ValueError("BTCUSDT research profile must be long-only and cash-only")
        if self.profile_version != "btcusdt-research-profile-v1":
            raise ValueError("unsupported research profile version")

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> CryptoResearchProfile:
        if not isinstance(raw, dict) or raw.get("schema_version") != "btcusdt-research-v1":
            raise ValueError("research config schema_version must be btcusdt-research-v1")
        account, fees, spread, slip = (
            raw.get(k) for k in ("account", "fees", "spread", "slippage")
        )
        if not all(isinstance(x, dict) for x in (account, fees, spread, slip)):
            raise ValueError("account, fees, spread, and slippage sections are required")
        required = {
            "account": {
                "account_id",
                "account_currency",
                "starting_capital",
                "max_risk_per_trade",
                "max_daily_loss",
                "max_concurrent_positions",
                "max_trades_per_utc_day",
                "long_only",
                "leverage_enabled",
                "borrowing_enabled",
                "short_selling_enabled",
            },
            "fees": {
                "maker_fee_bps",
                "taker_fee_bps",
                "fee_currency",
                "source",
                "configured_at",
                "classification",
            },
            "spread": {"full_bid_ask_bps", "source", "classification"},
            "slippage": {"additional_adverse_bps", "source", "classification"},
        }
        for name, required_fields in required.items():
            missing = required_fields - raw[name].keys()
            if missing:
                raise ValueError(f"{name} is missing required fields: {', '.join(sorted(missing))}")
        try:
            configured_at = (
                datetime.fromisoformat(fees["configured_at"]) if fees.get("configured_at") else None
            )
            fee_classification = (
                FeeClassification(fees["classification"]) if fees.get("classification") else None
            )
            spread_classification = (
                CostClassification(spread["classification"])
                if spread.get("classification")
                else None
            )
            slippage_classification = (
                CostClassification(slip["classification"]) if slip.get("classification") else None
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid research config enum or timestamp: {exc}") from exc
        return cls(
            account_id=str(account.get("account_id") or "btc-usdt-research"),
            account_currency=str(account.get("account_currency") or "USDT"),
            starting_capital=_optional_decimal(account.get("starting_capital"), "starting_capital"),
            max_risk_per_trade=_optional_decimal(
                account.get("max_risk_per_trade"), "max_risk_per_trade"
            ),
            max_daily_loss=_optional_decimal(account.get("max_daily_loss"), "max_daily_loss"),
            max_concurrent_positions=account.get("max_concurrent_positions"),
            max_trades_per_utc_day=account.get("max_trades_per_utc_day"),
            long_only=account.get("long_only") is True,
            leverage_enabled=account.get("leverage_enabled") is True,
            borrowing_enabled=account.get("borrowing_enabled") is True,
            short_selling_enabled=account.get("short_selling_enabled") is True,
            maker_fee_bps=_optional_decimal(fees.get("maker_fee_bps"), "maker_fee_bps"),
            taker_fee_bps=_optional_decimal(fees.get("taker_fee_bps"), "taker_fee_bps"),
            fee_currency=fees.get("fee_currency"),
            fee_source=fees.get("source"),
            fee_configured_at=configured_at,
            fee_classification=fee_classification,
            spread_bps=_optional_decimal(spread.get("full_bid_ask_bps"), "full_bid_ask_bps"),
            spread_source=spread.get("source"),
            spread_classification=spread_classification,
            slippage_bps=_optional_decimal(
                slip.get("additional_adverse_bps"), "additional_adverse_bps"
            ),
            slippage_source=slip.get("source"),
            slippage_classification=slippage_classification,
        )

    @classmethod
    def load_yaml(cls, path: str | Path) -> CryptoResearchProfile:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"research config is missing or invalid YAML: {exc}") from exc
        return cls.from_mapping(data)

    def snapshot(self) -> dict[str, Any]:
        values = asdict(self)
        values["fee_configured_at"] = (
            self.fee_configured_at.isoformat() if self.fee_configured_at else None
        )
        for name, value in tuple(values.items()):
            if isinstance(value, (D, StrEnum)):
                values[name] = str(value)
        return values

    @property
    def configuration_hash(self) -> str:
        return canonical_hash(self.snapshot())

    def blockers(self) -> list[str]:
        checks = (
            (self.starting_capital is not None, "ACCOUNT_CAPITAL_UNCONFIGURED"),
            (self.max_risk_per_trade is not None, "RISK_PER_TRADE_UNCONFIGURED"),
            (self.max_daily_loss is not None, "DAILY_LOSS_LIMIT_UNCONFIGURED"),
            (self.maker_fee_bps is not None, "MAKER_FEE_UNCONFIGURED"),
            (self.taker_fee_bps is not None, "TAKER_FEE_UNCONFIGURED"),
            (self.fee_currency == "USDT", "FEE_CURRENCY_UNSUPPORTED"),
            (self.spread_bps is not None, "SPREAD_UNCONFIGURED"),
            (self.slippage_bps is not None, "SLIPPAGE_UNCONFIGURED"),
            (
                self.fee_source is not None
                and self.fee_classification is not None
                and self.fee_configured_at is not None,
                "FEE_PROVENANCE_INCOMPLETE",
            ),
            (
                self.spread_source is not None and self.spread_classification is not None,
                "SPREAD_PROVENANCE_INCOMPLETE",
            ),
            (
                self.slippage_source is not None and self.slippage_classification is not None,
                "SLIPPAGE_PROVENANCE_INCOMPLETE",
            ),
            (self.max_concurrent_positions is not None, "CONCURRENT_POSITION_LIMIT_UNCONFIGURED"),
            (self.max_trades_per_utc_day is not None, "UTC_DAY_TRADE_LIMIT_UNCONFIGURED"),
        )
        return [code for passed, code in checks if not passed]

    def runtime_configs(self):
        """Build execution models only when every required user value is present."""
        missing = self.blockers()
        if missing:
            raise ValueError("research configuration incomplete: " + ", ".join(missing))
        assert self.starting_capital is not None
        assert self.max_risk_per_trade is not None and self.max_daily_loss is not None
        assert self.maker_fee_bps is not None and self.taker_fee_bps is not None
        assert self.spread_bps is not None and self.slippage_bps is not None
        assert self.fee_configured_at is not None and self.fee_currency is not None
        fee_config = CryptoSpotFeeConfiguration(
            self.maker_fee_bps / D(10000),
            self.taker_fee_bps / D(10000),
            self.fee_currency,
            self.fee_source or "",
            self.fee_configured_at,
            self.fee_classification == FeeClassification.ACTUAL_ACCOUNT_RATE,
            f"{self.profile_version}:{self.configuration_hash[:12]}",
        )
        account = CryptoSpotAccountConfig(
            self.account_id,
            "USDT",
            self.starting_capital,
            D(0),
            None,
            self.max_risk_per_trade,
            self.max_daily_loss,
            self.max_concurrent_positions or 1,
            self.max_trades_per_utc_day or 1,
            1440,
            "UTC",
            5,
        )
        costs = CryptoSpotCostAssumptions(
            fee_config.taker_model(),
            self.slippage_bps,
            self.spread_bps,
            "; ".join(x for x in (self.spread_source, self.slippage_source) if x),
            self.spread_classification == CostClassification.OBSERVED
            and self.slippage_classification == CostClassification.OBSERVED,
            f"{self.profile_version}:{self.configuration_hash[:12]}",
        )
        return account, fee_config, costs


@dataclass(frozen=True, slots=True)
class CryptoCostScenario:
    name: str
    fee_multiplier: Decimal
    slippage_multiplier: Decimal
    spread_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class CryptoCostScenarioResult:
    name: str
    fee_multiplier: Decimal
    slippage_multiplier: Decimal
    spread_multiplier: Decimal
    maker_fee_bps: Decimal | None
    taker_fee_bps: Decimal | None
    slippage_bps: Decimal | None
    spread_bps: Decimal | None
    profile_hash: str
    configuration_hash: str


SCENARIOS = (
    CryptoCostScenario("BASE", D("1.0"), D("1.0"), D("1.0")),
    CryptoCostScenario("COST_1_5X", D("1.5"), D("1.5"), D("1.0")),
    CryptoCostScenario("COST_2X", D("2.0"), D("2.0"), D("1.0")),
)


def cost_sensitivity_scenarios(
    profile: CryptoResearchProfile,
) -> tuple[CryptoCostScenarioResult, ...]:
    """Return exactly three immutable, hashed assumptions; never executes a run."""
    rows = []
    for scenario in SCENARIOS:
        values = {
            "name": scenario.name,
            "fee_multiplier": scenario.fee_multiplier,
            "slippage_multiplier": scenario.slippage_multiplier,
            "spread_multiplier": scenario.spread_multiplier,
            "maker_fee_bps": _scaled(profile.maker_fee_bps, scenario.fee_multiplier),
            "taker_fee_bps": _scaled(profile.taker_fee_bps, scenario.fee_multiplier),
            "slippage_bps": _scaled(profile.slippage_bps, scenario.slippage_multiplier),
            "spread_bps": _scaled(profile.spread_bps, scenario.spread_multiplier),
            "profile_hash": profile.configuration_hash,
        }
        values["configuration_hash"] = canonical_hash(values)
        rows.append(
            CryptoCostScenarioResult(
                **{
                    key: D(value)
                    if key in {"maker_fee_bps", "taker_fee_bps", "slippage_bps", "spread_bps"}
                    and value is not None
                    else value
                    for key, value in values.items()
                }
            )
        )
    return tuple(rows)


def _scaled(value: Decimal | None, factor: Decimal) -> Decimal | None:
    return value * factor if value is not None else None


def canonical_hash(value: Any) -> str:
    def canonical(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return str(item)
        if hasattr(item, "isoformat") and not isinstance(item, str):
            return item.isoformat()
        if hasattr(item, "__dataclass_fields__"):
            return canonical(asdict(item))
        if isinstance(item, dict):
            return {str(key): canonical(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [canonical(value) for value in item]
        return item

    payload = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def source_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
