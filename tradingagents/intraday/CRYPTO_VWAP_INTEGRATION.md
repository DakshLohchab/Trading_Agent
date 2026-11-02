# BTCUSDT VWAP simulation integration

## Data to candidate

`evaluate_crypto_market_data` accepts observed one-minute `MarketBar` records,
aggregates complete UTC-aligned 5-minute and 15-minute candles, computes the
existing causal indicators, and calls the separate
`crypto_vwap_trend_pullback` strategy. Its pinned configuration is
`crypto-vwap-ohlcv-1.0.0`: daily reset at 00:00 UTC, typical-price input
`(high + low + close) / 3`, Binance kline base-asset volume, and decisions only
at the completed candle end. The existing equity VWAP ID, configuration, and
candidate behavior are unchanged. ORB and range mean reversion remain disabled
for this path. Candidate IDs are deterministic and include the strategy
configuration and indicator snapshot. The 15-minute point is selected only
when its `available_at` is no later than the 5-minute decision time.

The Phase 4B manifest ID `BINANCE_SPOT:BTCUSDT` is an ingestion identity, not a
Nautilus instrument ID. `canonicalize_binance_spot_data` maps only that exact
known identity to `BTCUSDT.BINANCE_SPOT` and records the mapping in provenance.
It does not infer asset class or venue from a ticker.

## Nautilus execution and account ownership

`run_crypto_nautilus_execution` requires one-minute completed event bars,
unique and gap-free timestamps, a current parsed filter snapshot, an explicit
account, fee configuration, spread, and slippage. It invokes the isolated
`.venv-nautilus` worker and verifies NautilusTrader 1.231.0 at runtime. The
worker uses `CurrencyPair`, `AccountType.CASH`, multi-currency USDT/BTC opening
balances, and disabled cash borrowing. Candidate risk approval occurs at the
completed decision event. The stop-market entry is submitted at the next
one-minute event, so it cannot fill on its own decision candle. Exit orders are
market orders; when a one-minute OHLC candle touches both stop and target, the
stop is selected first.

Nautilus owns order status, fills, cash reservations, balances, and position
state. The custom `CryptoSpotAccountLedger` is used only through
`project_nautilus_fill` in this path. That method applies Nautilus fill events
as an independent Decimal accounting projection; it never reserves an order.
After the run, total/free/locked USDT and BTC, fill quantities, weighted
average fill prices, fees by currency, realized P&L, and final-mark unrealized
P&L are reconciled. Any detected difference raises an error. Orders with fees
in USDT are supported by this adapter; other fee currencies fail closed.

The adapter's `FillModel` uses NautilusTrader 1.231.0 deterministic one-tick
adverse slippage. Explicit spread and slippage inputs are also used by the risk
gate for conservative sizing. The OHLCV worker does not claim to simulate an
observed spread or order book, so its output always sets
`realistic_net_performance=false`. It does not call an LLM or exchange API.

## Current Binance Spot filters

The public endpoint was retrieved at `2026-11-02T17:07:33.792021Z`:

`https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT`

Binance server time in the response was `2026-11-02T17:06:39.572Z`. The symbol
was `TRADING`, base `BTC`, quote `USDT`, and its permission sets included
`SPOT`. Key values in this **current** snapshot:

- `PRICE_FILTER`: tick `0.01`; price range `0.01` to `1,000,000`.
- `LOT_SIZE`: step `0.00001`; minimum `0.00001`; maximum `9,000` BTC.
- `MARKET_LOT_SIZE`: min and step are zero (disabled in the response); maximum
  `122.91494833` BTC.
- `NOTIONAL`: minimum `5` USDT, maximum `9,000,000` USDT; minimum applies to
  market orders and maximum does not.
- `PERCENT_PRICE_BY_SIDE`: the raw record retains bid multipliers `0.5`–`1.2`,
  ask multipliers `0.8`–`2.0`, and a five-minute average window. The risk gate
  checks BUY trigger prices against the five latest completed one-minute
  closes when available. That is an OHLCV proxy for Binance's trade-weighted
  average and is not an exchange-equivalent validator.
- Base and quote asset precision are both 8. NautilusTrader 1.231.0 requires
  `size_precision` to equal the `size_increment` precision; the worker uses the
  filter step of five decimals for order quantities and bar-volume fields.
- The raw response retains `PERCENT_PRICE_BY_SIDE`, order-count and other
  symbol filters as returned by Binance.

The retained raw response is
[`BTCUSDT-exchangeInfo-raw.json`](/Users/dakshlohchab/nebius/Trading_Agent/tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-raw.json),
SHA-256 `f47dd9cae7c55b04273c305e3d66778a3c4afe47d0448ed4826544036bda248c`.
The parsed record is
[`BTCUSDT-exchangeInfo-normalized.json`](/Users/dakshlohchab/nebius/Trading_Agent/tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-normalized.json).
Both records are marked current-only and must not be represented as January
2025 rules. Binance publishes the Spot [`exchangeInfo` endpoint](https://developers.binance.com/en/docs/products/spot/rest-api)
and identifies account commission rates as signed `USER_DATA` in its
[Spot account API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-api/account).

## Dataset and historical-run gate

The existing Phase 4B BTCUSDT manifest was re-verified without changing the
data: dataset `819f13eb67b47c7a01634c10`, January 1–31, 2025 UTC, 44,640 one-
minute bars, 8,928 five-minute bars, 2,976 fifteen-minute bars, zero missing,
invalid, duplicate, or zero-volume intervals. Its normalized one-minute
Parquet SHA-256 is
`32caf8ca4805b16ac17c048f26a5b0693d102de0e89a3a928ef8c60e03d2b953`.

[`CRYPTO_READINESS.json`](/Users/dakshlohchab/nebius/Trading_Agent/tradingagents/intraday/CRYPTO_READINESS.json)
is the current preflight report. Dataset verification, current filters,
synthetic Nautilus integration, and synthetic ledger reconciliation pass.
Account capital/risk values, taker and maker rates, spread, and slippage are
not configured. Fee status is `COST_INCOMPLETE`; no historical strategy
backtest was run and the readiness gate remains closed.

## Data and fill limitations

The January dataset has OHLCV candles, not individual trades. VWAP is an
OHLCV-derived approximation, not trade-level exchange VWAP. Historical
order-book depth, observed bid/ask, queue position, market impact, and exact
intraminute trade sequence are unavailable. Stop/target outcomes within one
OHLCV candle are ambiguous; stop-first handling is a deterministic convention,
not a reconstruction of the exchange event sequence. The current filter
snapshot cannot establish January 2025 rules. No profitability result is
claimed.
