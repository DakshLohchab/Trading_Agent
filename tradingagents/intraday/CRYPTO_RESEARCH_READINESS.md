# BTCUSDT research configuration and readiness

`config/btcusdt_research.example.yaml` is a strict, separate USDT research
profile. Starting capital, risk limits, fees, spread, and slippage are `null`
until supplied. The profile never reads or converts the INR equity account.
Fee classifications distinguish `ACTUAL_ACCOUNT_RATE` from
`RESEARCH_ASSUMPTION`; spread and slippage distinguish `OBSERVED` from
`RESEARCH_ASSUMPTION`. Only USDT fees are accepted by the Nautilus path.

Check a retained manifest without running historical strategy execution:

```sh
.venv/bin/python -m tradingagents.intraday.crypto_readiness \
  --manifest /explicit/path/to/dataset-manifest.json \
  --config tradingagents/intraday/config/btcusdt_research.example.yaml \
  --filters tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-normalized.json
```

The command verifies dataset manifest identity, raw archive and sidecar
checksums, normalized Parquet hashes and row counts, timestamps, January
interval counts, gaps, and reported quality. It re-parses the retained raw
`exchangeInfo` response and checks both its SHA-256 and normalized record. The
filter record is explicitly marked **current-only**; it does not establish
January 2025 exchange rules. Missing data returns `BLOCKED_DATA`; mismatched
hashes fail closed.

The readiness command always runs the strategy-to-Nautilus path on an
explicitly synthetic, temporary dataset when the retained current filter
snapshot verifies. That self-check validates candidate generation, temporal
availability, risk-before-order sequencing, simulated fills, configured fill
costs, USDT taker commissions, and ledger reconciliation. It emits no
historical performance metrics and does not run the retained January dataset.

## Execution costs

`CryptoExecutionCostModel` is the shared Decimal implementation used by both
`approve_crypto_candidate(...)` and Nautilus' Python `FillModel` hook for pinned
version 1.231.0. Nautilus supplies the current simulated execution event's
reference price. The fill model creates one synthetic L2 liquidity level at
the adjusted price and sets Nautilus' independent probabilistic one-tick
slippage to zero. Buys round up to the instrument tick; sells round down. The
ledger projects Nautilus' actual fill price and returned commission. It does
not charge spread, slippage, or fees a second time.

The fee model uses the instrument's maker/taker rates and Nautilus charges the
commission on actual filled notional. Stop-market entries and market exits are
classified from Nautilus' fill event; the current strategy uses taker fills.
The OHLCV reference is a simulated event price, not an observed historical
bid/ask. This model does not imply order-book realism or live-trading
suitability.

The cost sensitivity API produces exactly three frozen, hashed scenarios:

| Scenario | Fee multiplier | Slippage multiplier | Spread multiplier |
|---|---:|---:|---:|
| `BASE` | 1.0x | 1.0x | 1.0x |
| `COST_1_5X` | 1.5x | 1.5x | 1.0x |
| `COST_2X` | 2.0x | 2.0x | 1.0x |

They are configuration outputs only. The readiness command does not execute
historical scenario runs.

## Reproducibility and status

The report hashes canonical sorted JSON containing dataset and artifact hashes,
instrument and current filter metadata, strategy/VWAP settings, user research
profile, sensitivity scenarios, cost model, Nautilus version, and relevant
implementation source hashes. Decimal values serialize as strings. Identical
inputs produce the same hash; changed assumptions or source bytes change it.

Statuses are `READY_FOR_RESEARCH_BACKTEST`, `BLOCKED_CONFIGURATION`,
`BLOCKED_DATA`, and `BLOCKED_INTEGRATION`. The report retains all blockers even
when the overall status selects one category. A ready research configuration
does not claim strategy edge, profitability, or live suitability.
