# Crypto spot execution boundary

## Separation from equities

`RiskLimits`, `approve_candidate`, and the existing `run_nautilus_execution`
worker remain the INR cash-equity path. Crypto uses the separate
`CryptoSpotExecutionSpec`, `CryptoSpotAccountConfig`,
`CryptoSpotAccountLedger`, `CryptoSpotFeeModel`, and
`approve_crypto_candidate` interfaces. No LLM or broker client is imported.
The Nautilus historical bar-delivery adapter uses a CASH account for crypto,
requires explicit base/quote balances, sets `base_currency=None`, and disables
cash borrowing. Equity initialization remains unchanged.

The bar-delivery adapter only validates and delivers historical data; it does
not submit crypto orders. Order accounting is exposed through the isolated
simulation ledger and fill boundary. A full strategy-to-Nautilus crypto order
worker remains a separate integration task.

## Instrument, account, and risk

The instrument must be an explicit `crypto_spot` `InstrumentMetadata` with a
24/7 calendar, base and quote currencies, price tick, quantity step, minimum
quantity, minimum notional, source, and provenance. `CryptoSpotExecutionSpec`
adds account currency, quantity precision, fee currency/model ID, instrument
specification source, and metadata retrieval time. It rejects account/quote
currency mismatch; it does not perform FX conversion.

`CryptoSpotAccountConfig` requires a distinct account ID, explicit quote/base
starting balances and cost basis if opening base inventory exists, risk per
trade, max daily loss, position/trade limits, risk period, and maximum data age.
There are no crypto defaults and no reuse of the INR ₹10,000 portfolio. The
risk gate requires a valid long candidate, completed and fresh data, explicit
instrument rules, a fee model, explicit spread/slippage inputs, USDT account
currency, and sufficient quote balance. Order reservation/fill accounting also
requires available quote funds for buys or base inventory for sells. Borrowing,
margin, negative balances, negative inventory, implicit third-currency fees,
and silent INR conversion are rejected.

The append-only in-memory simulation ledger records balance reservations,
released reservations, fills, fee amount/currency, realized quote-currency P&L,
and the current cost basis. Base-denominated fees reduce BTC inventory; quote
fees debit/credit USDT. A fill must occur on a later event than submission.
Price tick, quantity step, minimum quantity, and minimum notional are enforced.

## Fees and execution costs

No Binance fee rate is hardcoded. The current Binance account commission API is
a signed USER_DATA endpoint requiring an API key; no Binance credentials were
provided or queried. Public Spot `exchangeInfo` filter rules describe price
ticks, quantity steps, and minimum notional, but this phase did not retrieve
BTCUSDT's current symbol filters. Supply those exact values with source and
retrieval time before any simulation. See the [Binance Spot REST API](https://developers.binance.com/en/docs/products/spot/rest-api),
[account commission endpoint](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-api/account),
and [Spot REST security](https://developers.binance.com/en/docs/products/spot/rest-api).

`CryptoSpotFeeModel` requires a fee rate, fee currency, source, version, and an
explicit `account_specific` flag. User assumptions can be represented for
synthetic software tests, but they report `COST_INCOMPLETE`; they are not
described as Binance's actual fee tier. `CryptoSpotCostAssumptions` requires
explicit slippage and spread values. Missing fees or execution costs reject a
risk decision. It does not infer BNB discounts or use a third fee currency.

## Calendar and VWAP

The crypto market uses the continuous `24/7` calendar. Analytical session
anchors remain explicit UTC strategy configuration. The existing equity VWAP
is unchanged. No crypto-specific VWAP variant is introduced in this phase.
Existing OHLCV indicators label VWAP as `ohlcv_typical_price_approximation`;
individual trade prints are required for exact trade-level VWAP. ORB remains
disabled for crypto.

## Remaining blockers

- A retained Binance BTCUSDT filter snapshot is available, but is current-only;
  it does not establish January 2025 exchange rules. The research readiness
  gate verifies its raw SHA-256 and retrieval timestamp.
- Account capital, risk limits, fees, spread, and slippage remain unset in the
  research profile until supplied. Explicit research assumptions remain labeled
  as assumptions and do not claim actual Binance account rates.
- `run_crypto_experiment(...)` connects the strategy candidate pipeline to the
  isolated Nautilus CASH worker. Historical execution remains gated on verified
  dataset, configuration, and readiness inputs; Phase 4D.3 does not run it.
- The Binance source dataset is local and governed by its noncommercial license;
  do not commit or redistribute it.
