# BTCUSDT research readiness

Status: **BLOCKED_DATA**
Experiment configuration hash: `2a421a1216f375534419150dcf8aadb2c4902ea19e5331ed5643fa2d3c4aef37`

The deterministic synthetic Nautilus integration check is software validation only. No historical performance is reported.

## Blockers

- `ACCOUNT_CAPITAL_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `RISK_PER_TRADE_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `DAILY_LOSS_LIMIT_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `MAKER_FEE_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `TAKER_FEE_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `SPREAD_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `SLIPPAGE_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `FEE_PROVENANCE_INCOMPLETE` (configuration): required research input is unset or unsupported
- `SPREAD_PROVENANCE_INCOMPLETE` (configuration): required research input is unset or unsupported
- `SLIPPAGE_PROVENANCE_INCOMPLETE` (configuration): required research input is unset or unsupported
- `CONCURRENT_POSITION_LIMIT_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `UTC_DAY_TRADE_LIMIT_UNCONFIGURED` (configuration): required research input is unset or unsupported
- `EXECUTION_COST_MODEL_INCOMPLETE` (configuration): taker fee, spread, and slippage must all be explicitly configured
- `DATASET_MANIFEST_MISSING` (data): manifest is missing or invalid JSON
- `HISTORICAL_FILTER_COMPATIBILITY_UNVERIFIED` (configuration): retained Binance filters are current-only and do not establish January 2025 rules

## Dataset and filter

- Dataset: blocked
- Current filter snapshot SHA-256: `f47dd9cae7c55b04273c305e3d66778a3c4afe47d0448ed4826544036bda248c`
- Filter retrieved at: `2026-11-02T17:07:33.792021+00:00`
- Filter scope: current exchange rules only; not evidence of January 2025 rules.

## Synthetic self-check

- Executed: `True`
- Status: `COMPLETE`
- Fills: `4`
- Ledger reconciliation: `True`
