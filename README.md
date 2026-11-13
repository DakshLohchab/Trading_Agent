# TradingAgents

A multi-agent LLM financial trading framework and execution platform for quantitative research, algorithmic analysis, and multi-asset trading.

TradingAgents orchestrates specialized teams of autonomous agents to evaluate market data, fundamentals, and sentiment, run rigorous risk debates, and execute trade decisions across equity and crypto markets.

---

## Architecture Overview

```
                                      ┌───────────────┐
                                      │  Market Data  │
                                      │ & News Feeds  │
                                      └───────┬───────┘
                                              │
                      ┌───────────────────────┼───────────────────────┐
                      ▼                       ▼                       ▼
             ┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
             │ Market Analyst  │    │ Sentiment Analyst│    │   News Analyst   │
             │  (Technicals)   │    │ (News & Socials) │    │  (Macro & News)  │
             └────────┬────────┘    └────────┬─────────┘    └────────┬─────────┘
                      │                      │                       │
                      └──────────────────────┼───────────────────────┘
                                             │
                                             ▼
                                  ┌────────────────────┐
                                  │Fundamentals Analyst│
                                  │(Equities Financial)│
                                  └──────────┬─────────┘
                                             │
                      ┌──────────────────────┴──────────────────────┐
                      ▼                                             ▼
             ┌─────────────────┐                           ┌─────────────────┐
             │ Bull Researcher │                           │ Bear Researcher │
             └────────┬────────┘                           └────────┬─────────┘
                      │                                             │
                      └──────────────────────┬──────────────────────┘
                                             │ (Structured Debate)
                                             ▼
                                  ┌────────────────────┐
                                  │    Trader Agent    │
                                  │ (Proposal & Sizing)│
                                  └──────────┬─────────┘
                                             │
                                             ▼
                                  ┌────────────────────┐
                                  │  Risk Management   │
                                  │ (Volatility/Limit) │
                                  └──────────┬─────────┘
                                             │
                                             ▼
                                  ┌────────────────────┐
                                  │ Portfolio Manager  │
                                  │(Approval/Execution)│
                                  └──────────┬─────────┘
                                             │
                                             ▼
                                  ┌────────────────────┐
                                  │  Execution Engine  │
                                  │(Nautilus / Brokers)│
                                  └────────────────────┘
```

### Specialized Agent Teams

1. **Analyst Team**
   - **Market / Technical Analyst**: Calculates technical indicators (MACD, RSI, Moving Averages, Bollinger Bands) and detects chart patterns.
   - **Sentiment Analyst**: Analyzes sentiment from headlines, StockTwits, and social chatter.
   - **News Analyst**: Monitors macroeconomic events, geopolitical news, and corporate developments.
   - **Fundamentals Analyst**: Evaluates financial statements, balance sheets, cash flows, and valuation metrics (auto-disabled for crypto).

2. **Researcher Team**
   - **Bullish & Bearish Researchers**: Engage in structured debates weighing opportunities against downside risks.

3. **Trader & Risk Management**
   - **Trader Agent**: Synthesizes research outputs into clear, actionable trade proposals.
   - **Risk Management Debaters**: Stress-test proposals with conservative, neutral, and aggressive risk perspectives.
   - **Portfolio Manager**: Approves or rejects transactions based on liquidity, exposure, and risk guidelines.

4. **Execution Engine & Intraday Trading**
   - Backtesting & live simulated execution.
   - High-performance execution via NautilusTrader.
   - Native integration for crypto (Binance) and equities (NSE/Upstox, US/Alpaca, Yahoo Finance).

---

## Getting Started

### Installation

Requires Python 3.11+.

```bash
# Clone the repository
git clone https://github.com/your-username/Trading_Agent.git
cd Trading_Agent

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .
```

### Configuration

Copy the example environment file and add your API keys:

```bash
cp .env.example .env
```

Supported LLM providers include OpenAI, Anthropic, Google Gemini, DeepSeek, xAI, OpenRouter, and local models via Ollama.

---

## Python API Usage

TradingAgents provides a clean programmatic interface designed for integration into custom scripts, pipelines, and upcoming Web UI dashboards.

### Basic Analysis Run

```python
from tradingagents import TradingAgentsGraph, DEFAULT_CONFIG

# Load configuration with environment variable overlays
config = DEFAULT_CONFIG.copy()

# Initialize the multi-agent graph
agent = TradingAgentsGraph(debug=True, config=config)

# Run propagation for an asset
final_state, decision = agent.propagate(
    company_name="NVDA",
    trade_date="2026-09-01",
    asset_type="stock"  # or "crypto"
)

print("Trade Decision:", decision)
```

### Crypto Day Trading (Live & Paper)

Run the autonomous Crypto Day Trading engine on Binance Spot (default: `BTCUSDT`):

```bash
# 1. Instant real-time scan of BTCUSDT VWAP, EMAs, 15m trend, and pullback setups
python crypto_trader.py --scan

# 2. Start continuous Paper Day Trading on live market data (simulated fills)
python crypto_trader.py --paper

# 3. Start Live Day Trading on Binance (requires BINANCE_API_KEY & BINANCE_API_SECRET in .env)
python crypto_trader.py --live --risk 50
```

Programmatic Python usage:

```python
from tradingagents import CryptoDayTrader

# Initialize paper or live trader
trader = CryptoDayTrader(symbol="BTCUSDT", mode="paper")

# Instant market scan
status = trader.scan()
print("Current Price:", status["current_price"])
print("00:00 UTC VWAP:", status["vwap"])
print("Setup Status:", status["setup_status"])

# Run live monitoring and execution loop
# trader.run_live_loop(poll_interval_seconds=15)
```

---

## Project Structure

```
Trading_Agent/
├── main.py                     # Entry point for direct execution
├── pyproject.toml              # Project dependencies and build configuration
├── requirements.txt            # Package requirements reference
├── .env.example                # Example environment configuration
├── tests/                      # Comprehensive test suite
│   ├── intraday/               # Tests for intraday, execution, and pilot engines
│   └── ...                     # Core agent and client unit tests
└── tradingagents/              # Core TradingAgents framework
    ├── agents/                 # Multi-agent role definitions & tools
    │   ├── analysts/           # Market, Sentiment, News, Fundamentals
    │   ├── managers/           # Portfolio & Research Managers
    │   ├── researchers/        # Bull & Bear debaters
    │   ├── risk_mgmt/          # Risk management team
    │   └── trader/             # Trader agent implementation
    ├── dataflows/              # Market data routing & vendor clients
    │   ├── vendors/            # Yahoo, Alpha Vantage, SEC EDGAR, FRED, etc.
    │   └── symbols.py          # Symbol normalization and classification
    ├── graph/                  # LangGraph state machine & propagation
    ├── intraday/               # High-frequency, Upstox, NSE, and Crypto execution
    ├── llm_clients/            # Multi-provider LLM abstraction layer
    ├── memory/                 # Decision logs and reflective memory
    ├── backtest.py             # Backtesting engine
    └── default_config.py       # Default configurations & env loading
```

---

## Running Tests

Run the test suite:

```bash
pytest
```
