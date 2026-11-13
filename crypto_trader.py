#!/usr/bin/env python3
"""Crypto Day Trading Command Runner.

Executes systematic, risk-managed crypto day trading across Paper Trading and Live Binance Spot modes.

Examples:
    # 1. Instant market scan on current BTCUSDT conditions & setups:
    python crypto_trader.py --scan

    # 2. Start continuous paper day trading (simulated fills on live market data):
    python crypto_trader.py --paper

    # 3. Start live trading on Binance Spot (requires BINANCE_API_KEY and BINANCE_API_SECRET in .env):
    python crypto_trader.py --live --risk 50
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from tradingagents.intraday.crypto_day_trader import CryptoDayTrader

D = Decimal


def print_banner(mode: str, symbol: str) -> None:
    print("=" * 70)
    print(f"  TRADINGAGENTS — CRYPTO DAY TRADER [{mode.upper()}]")
    print(f"  Target Instrument: {symbol} | Session: 24/7 Continuous (00:00 UTC Anchor)")
    print("=" * 70)


def run_scan(trader: CryptoDayTrader) -> None:
    print_banner("MARKET SCAN", trader.symbol)
    print("\nFetching latest completed candles and computing VWAP & indicators...")
    data = trader.scan()

    print(f"\n--- {data['symbol']} Market Overview ({data['timestamp']}) ---")
    print(f"• Current Price      : ${data['current_price']}")
    if data.get("latest_5m_close"):
        print(f"• Latest 5m Close    : ${data['latest_5m_close']} (Bar End: {data['latest_5m_bar_end']})")
    if data.get("vwap"):
        vwap_val = D(data['vwap'])
        price_val = D(data['current_price'])
        diff_pct = ((price_val - vwap_val) / vwap_val) * 100
        sign = "+" if diff_pct >= 0 else ""
        print(f"• 00:00 UTC VWAP     : ${vwap_val:.2f} ({sign}{diff_pct:.2f}% from current price)")
    if data.get("ema20"):
        print(f"• 5m 20 EMA          : ${D(data['ema20']):.2f}")
    if data.get("ema50"):
        print(f"• 5m 50 EMA          : ${D(data['ema50']):.2f}")
    print(f"• 15m Trend Status   : {data['trend_15m']}")
    print(f"• Setup Evaluation   : {data['setup_status']} ({data.get('setup_reason') or 'No reason provided'})")

    candidate = data.get("candidate")
    if candidate:
        print("\n🎯 [ACTIVE TRADE SIGNAL DETECTED]")
        print(f"• Candidate ID       : {candidate['candidate_id']}")
        print(f"• Direction          : {candidate['direction']}")
        print(f"• Entry Trigger      : ${candidate['entry_trigger_price']}")
        print(f"• Stop Loss          : ${candidate['stop_price']}")
        print(f"• Profit Target      : ${candidate['target_price']}")
        print(f"• Reward/Risk Ratio  : {candidate['risk_reward_ratio']}")
    else:
        print("\n⏳ No actionable trade candidate on current completed 5m bar.")
        print("  Strategy rule: Requires 15m bullish trend confirmation and 5m VWAP pullback.")

    print(f"\nAccount Cash: {data['cash_balance']} USDT | Realized P&L Today: {data['daily_realized_pnl']} USDT")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradingAgents: Crypto Day Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="Perform an instant market scan and signal check.")
    group.add_argument("--paper", action="store_true", help="Start continuous real-time paper trading.")
    group.add_argument("--live", action="store_true", help="Start live trading on Binance Spot.")

    parser.add_argument("--symbol", default="BTCUSDT", help="Trading symbol (default: BTCUSDT).")
    parser.add_argument("--config", default=None, help="Path to custom day trading YAML configuration.")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval in seconds (default: 15).")
    parser.add_argument("--capital", type=float, default=None, help="Override starting capital (USDT).")
    parser.add_argument("--risk", type=float, default=None, help="Override risk per trade (USDT).")

    args = parser.parse_args()

    mode = "live" if args.live else "paper"
    trader = CryptoDayTrader(symbol=args.symbol, mode=mode, config_path=args.config)

    if args.capital is not None:
        trader.state.starting_capital = D(str(args.capital))
        trader.state.current_cash = D(str(args.capital))
    if args.risk is not None:
        object.__setattr__(trader.account_config, "max_risk_per_trade", D(str(args.risk)))

    if args.scan:
        run_scan(trader)
    else:
        print_banner(mode, trader.symbol)
        trader.run_live_loop(poll_interval_seconds=args.interval)


if __name__ == "__main__":
    main()
