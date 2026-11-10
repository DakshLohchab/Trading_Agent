# BTCUSDT crypto experiment runner

## Executable call chain

`run_crypto_experiment(manifest_path, instrument, ...)` is the orchestration
entry point in `crypto_experiment.py`. It follows the existing data and
execution boundaries:

1. `verify_dataset_manifest(...)` validates the manifest, raw inputs, Parquet
   artifact hashes, counts, schema, and interval quality.
2. `one_minute_path(...)` selects the manifest's January 2025 one-minute
   artifact. `read_market_data(...)` loads it without sorting or filling rows.
3. The known Phase 4B identity `BINANCE_SPOT:BTCUSDT` is mapped through
   `canonicalize_binance_spot_data(...)` to `BTCUSDT.BINANCE_SPOT`.
4. `evaluate_crypto_market_data(...)` calls `aggregate_bars(...)` for completed
   5-minute and 15-minute candles, computes indicators with the configured
   00:00 UTC VWAP reset, then calls
   `evaluate_crypto_vwap_trend_pullback(...)` to create candidates.
5. The orchestrator sorts and deduplicates valid candidates by decision time
   and candidate ID, then hands the candidates and observed one-minute bars to
   `run_crypto_nautilus_execution(...)`.
6. In its Nautilus 1.231.0 subprocess, `CryptoStrategy.on_bar(...)` evaluates a
   candidate only on its completed decision event. Risk approval runs before
   order construction. Entry submission is delayed until a later one-minute
   event. Nautilus order and fill callbacks return through the worker result.
7. `on_order_filled(...)` projects each authoritative Nautilus fill through
   `CryptoSpotAccountLedger.project_nautilus_fill(...)`. The worker compares
   balances, locks, position, fees, and P&L to Nautilus after execution.
8. The orchestrator writes deterministic JSON and Markdown reports under the
   requested output directory.

The worker receives the candidate list up front as serialized input, but its
strategy exposes each candidate only at the matching completed decision event;
there is no earlier risk decision for a future candidate. An entry order cannot
be submitted on that decision event, and Nautilus can fill it only on a
subsequent market-data event. Candidate expiry and the existing stop-first
OHLCV exit convention remain in the worker.

## Interface

```python
run_crypto_experiment(
    manifest_path,
    instrument,
    mode="SYNTHETIC_SELF_CHECK" or "HISTORICAL",
    output_directory=...,  # required
    execution_spec=...,    # required to execute a simulation
    account=...,
    fee_configuration=...,
    costs=...,
    strategy_config=...,   # optional; defaults to CryptoVWAPPullbackConfig
    historical_readiness=...,  # required for HISTORICAL
)
```

The manifest path is explicit and can point to any location. Missing manifests,
missing raw or Parquet artifacts, checksum errors, incompatible instrument
identity, invalid timestamps, and data gaps produce a persisted `BLOCKED_DATA`
report. The known 1-minute artifact remains selected through the existing
`one_minute_path(...)` contract; no data is downloaded, sorted, repaired, or
forward-filled.

Reports include deterministic run and strategy configuration hashes, dataset
hashes, strategy and VWAP conventions, candidate/rejection counts, risk
decisions, order events, fills, fees, opening/ending balances, reconciliation,
cost/execution model IDs, readiness, and limitations. Synthetic reports do not
include historical P&L or profitability metrics.

## Execution authority

Nautilus owns simulated orders, order status, fills, positions, CASH balances,
and reservations. `approve_crypto_candidate(...)` is the pre-trade risk gate.
The custom crypto ledger is used by this path only as a projection of Nautilus
fills and locked balances; it does not reserve or debit funds in the
orchestration path. Reconciliation compares the projection with Nautilus
account state. Balances, locks, positions, and fees are compared directly.
Cumulative P&L is reconciled from Nautilus ending CASH balances and marked BTC
inventory against projected realized plus unrealized P&L. The scalar
`Portfolio.realized_pnl` value is retained in the result as an additional
diagnostic; cumulative reconciliation allows a documented `0.0000001 USDT`
Decimal tolerance for calculation precision.

There is no live exchange order path in this runner. `CryptoExecutionCostModel`
is shared by risk sizing and the Nautilus 1.231.0 fill-model hook. The hook uses
the simulated execution event's reference price, applies half-spread plus
adverse slippage, rounds to the instrument tick, and supplies one synthetic
liquidity level. Nautilus' separate probabilistic one-tick slippage is disabled.
The worker records reference and adjusted prices; the ledger projects the
actual Nautilus fill and commission once. OHLCV contains no observed historical
bid/ask or depth, so outputs do not claim realistic net performance.

See `CRYPTO_RESEARCH_READINESS.md` for the unset research profile, hashed cost
scenarios, dataset/filter validation, experiment hash, and readiness command.
The command runs only a temporary synthetic integration self-check.

## Modes and readiness

### `SYNTHETIC_SELF_CHECK`

This mode accepts only a manifest marked `provider: synthetic`. Synthetic
fixtures exercise the real manifest verifier, data loader, candidate pipeline,
Nautilus worker, fill callback, projection, reconciliation, and report output.
Their fee/spread/slippage settings are explicitly synthetic test inputs and
remain `COST_INCOMPLETE`; results are software integration evidence, not trading
performance evidence.

### `HISTORICAL`

This mode requires a valid dataset, complete account/risk inputs, account
specific fees, verified spread and slippage, an execution cost model marked
validated, and readiness flags for Nautilus integration and ledger
reconciliation. Missing or false readiness values produce `BLOCKED_READINESS`.
The historical mode was not run during Phase 4D.2.2. No historical backtest is
authorized by this synthetic integration work.

The January 2025 manifest path previously recorded in `CRYPTO_READINESS.json`
is currently absent from this checkout. The retained Binance filter snapshot
is current-only and does not establish January 2025 filters. Historical work
remains gated on the dataset being provisioned and verified, complete explicit
risk/cost configuration, and the separate Phase 4D.3 readiness/configuration
work.
