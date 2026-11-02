"""Deterministic performance summaries for time-ordered simulated results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev

D = Decimal


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    strategy_id: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_notional: Decimal
    exit_notional: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage: Decimal

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees - self.slippage


@dataclass(frozen=True, slots=True)
class EquityMark:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    market_exposure: Decimal
    is_session_close: bool = True


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    starting_capital: Decimal
    ending_equity: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    profit_factor: Decimal | None
    expectancy_per_trade: Decimal | None
    maximum_drawdown: Decimal
    sharpe: float | None
    sortino: float | None
    average_holding_minutes: Decimal | None
    average_exposure: Decimal | None
    turnover: Decimal
    rejected_candidates: int
    rejected_orders: int
    risk_limit_violations: int
    annualization: str
    risk_free_rate: Decimal


def calculate_metrics(
    *,
    starting_capital: Decimal,
    trades: Iterable[ClosedTrade],
    equity_marks: Iterable[EquityMark],
    rejected_candidates: int = 0,
    rejected_orders: int = 0,
    risk_limit_violations: int = 0,
    risk_free_rate: Decimal | None = None,
    trading_sessions_per_year: int = 252,
) -> PerformanceMetrics:
    """Use session-close equity marks for Sharpe/Sortino (252 sessions, rf=0 default)."""
    trades = tuple(trades)
    marks = tuple(sorted(equity_marks, key=lambda mark: mark.timestamp))
    close_marks = tuple(mark for mark in marks if mark.is_session_close)
    risk_free_rate = D(0) if risk_free_rate is None else D(str(risk_free_rate))
    if not risk_free_rate.is_finite() or risk_free_rate < 0:
        raise ValueError("risk-free rate must be finite and non-negative")
    if starting_capital <= 0 or trading_sessions_per_year <= 0:
        raise ValueError("starting capital and annualization sessions must be positive")
    if any(mark.equity < 0 for mark in marks):
        raise ValueError("equity marks cannot be negative")
    gross = sum((trade.gross_pnl for trade in trades), D(0))
    fees = sum((trade.fees for trade in trades), D(0))
    slippage = sum((trade.slippage for trade in trades), D(0))
    net = gross - fees - slippage
    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross_wins = sum(wins, D(0))
    gross_losses = -sum(losses, D(0))
    peak = starting_capital
    max_drawdown = D(0)
    for mark in marks:
        peak = max(peak, mark.equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - mark.equity) / peak)
    # One return per local session close; caller supplies only actual close marks.
    daily_returns = []
    previous = starting_capital
    previous_date = None
    for mark in close_marks:
        current_date = mark.timestamp.date()
        if previous_date == current_date:
            continue
        daily_returns.append(float(mark.equity / previous - 1))
        previous = mark.equity
        previous_date = current_date
    excess = [value - float(risk_free_rate) / trading_sessions_per_year for value in daily_returns]
    sharpe = None
    if len(excess) > 1 and pstdev(excess) > 0:
        sharpe = mean(excess) / pstdev(excess) * sqrt(trading_sessions_per_year)
    downside = [min(value, 0.0) for value in excess]
    downside_deviation = sqrt(mean(value * value for value in downside)) if downside else 0.0
    sortino = (
        mean(excess) / downside_deviation * sqrt(trading_sessions_per_year)
        if downside_deviation > 0
        else None
    )
    ending = marks[-1].equity if marks else starting_capital + net
    durations = [
        D(str((trade.exit_timestamp - trade.entry_timestamp).total_seconds() / 60))
        for trade in trades
    ]
    exposure = [mark.market_exposure / mark.equity for mark in marks if mark.equity > 0]
    return PerformanceMetrics(
        starting_capital,
        ending,
        gross,
        net,
        fees,
        slippage,
        len(trades),
        len(wins),
        len(losses),
        D(len(wins)) / len(trades) if trades else None,
        sum(wins, D(0)) / len(wins) if wins else None,
        sum(losses, D(0)) / len(losses) if losses else None,
        gross_wins / gross_losses if gross_losses else None,
        net / len(trades) if trades else None,
        max_drawdown,
        sharpe,
        sortino,
        sum(durations, D(0)) / len(durations) if durations else None,
        sum(exposure, D(0)) / len(exposure) if exposure else None,
        sum((trade.entry_notional + trade.exit_notional for trade in trades), D(0)),
        rejected_candidates,
        rejected_orders,
        risk_limit_violations,
        f"daily close returns; sqrt({trading_sessions_per_year}); rf={risk_free_rate} annualized",
        risk_free_rate,
    )
