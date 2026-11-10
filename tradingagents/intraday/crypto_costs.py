"""Shared deterministic BTC spot execution-cost calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

D = Decimal
ExecutionSide = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class CryptoExecutionCostModel:
    """Apply full spread and additional adverse slippage once to a reference price."""

    spread_bps: Decimal
    slippage_bps: Decimal
    version: str = "crypto-bps-cost-model-v1"

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps"):
            value = D(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if not self.version.strip():
            raise ValueError("execution cost model version is required")

    def adjustment(self, reference_price: Decimal) -> Decimal:
        price = D(str(reference_price))
        if not price.is_finite() or price <= 0:
            raise ValueError("reference fill price must be finite and positive")
        half_spread = price * self.spread_bps / D(20000)
        slippage = price * self.slippage_bps / D(10000)
        return half_spread + slippage

    def adjusted_price(
        self, reference_price: Decimal, side: ExecutionSide, tick_size: Decimal
    ) -> Decimal:
        price, tick = D(str(reference_price)), D(str(tick_size))
        if side not in ("BUY", "SELL"):
            raise ValueError("execution side must be BUY or SELL")
        if not price.is_finite() or price <= 0 or not tick.is_finite() or tick <= 0:
            raise ValueError("reference price and tick size must be finite and positive")
        adverse = self.adjustment(price)
        raw = price + adverse if side == "BUY" else price - adverse
        if raw <= 0:
            raise ValueError("adverse sell adjustment produced a non-positive price")
        rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
        return (raw / tick).to_integral_value(rounding=rounding) * tick


def quote_fee(notional: Decimal, fee_rate: Decimal) -> Decimal:
    """Conservatively estimate a USDT fee to its eight-decimal currency precision."""
    amount, rate = D(str(notional)), D(str(fee_rate))
    if not amount.is_finite() or amount < 0 or not rate.is_finite() or rate < 0:
        raise ValueError("notional and fee rate must be finite and non-negative")
    return (amount * rate).quantize(D("0.00000001"), rounding=ROUND_CEILING)
