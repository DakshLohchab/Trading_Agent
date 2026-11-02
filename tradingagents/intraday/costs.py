"""No-assumption execution-cost boundary for future evaluators."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CostEstimate:
    available: bool
    estimated_net_return: Decimal | None
    currency: str | None
    source: str
    components: tuple[tuple[str, Decimal | None], ...] = ()


class CostEstimator(Protocol):
    def estimate(
        self, *, entry: Decimal, exit_price: Decimal, instrument_id: str
    ) -> CostEstimate: ...


class UnavailableCostEstimator:
    """Explicitly reports unavailable costs; it never substitutes guessed rates."""

    def estimate(self, *, entry: Decimal, exit_price: Decimal, instrument_id: str) -> CostEstimate:
        return CostEstimate(False, None, None, "cost_inputs_not_configured")
