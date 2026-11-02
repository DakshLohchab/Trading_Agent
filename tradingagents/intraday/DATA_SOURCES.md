# Historical data acquisition (Phase 4A)

This package acquires historical data only. It does not expose order, account, or
live trading APIs. India, US equities, and Binance spot are kept in separate
instrument namespaces and dataset partitions.

## India: Upstox V3

`UpstoxHistoricalClient` uses the V3 minute candle endpoint. Supply a valid
entitled access token through `UPSTOX_ACCESS_TOKEN`; it is read at request time
and is never written into a manifest. Resolve current NSE cash-equity keys with
`resolve_nse_equity()` from the official daily NSE instrument master. The
resolver accepts only one exact `NSE_EQ`/`EQ` match and preserves the master
file SHA-256 and raw tick-size field. It does not infer a tick-size unit.

Minute requests are split into inclusive calendar-month chunks, consistent
with the provider's documented maximum request range for 1–15 minute candles.
The provider documents minute history from January 2022. The optional
`raw_cache_dir` on `fetch_range()` stores successful month responses atomically;
rerunning the same range parses completed chunks from cache and fetches only
missing months. A malformed cache fails closed and should be inspected rather
than overwritten. Use a user-local directory outside the checkout, for example
`~/.local/share/tradingagents/upstox-raw`.

The client limits requests to 10 per second, retries network/429/5xx failures
with bounded backoff and honors `Retry-After`. It does not retry ordinary 4xx
responses, bypass entitlements, or attempt to backfill dates before January
2022. The Upstox standard API limit is higher, but the local limit is
intentionally conservative. The pilot symbols RELIANCE, TCS, INFY, HDFCBANK,
and ICICIBANK are examples only: always resolve their current identifiers from
the daily instrument master. A present-day set is a limited research universe,
not point-in-time index membership. Delisted instruments, historical symbol
changes, index membership, and corporate actions require separate authoritative
history. No prices are adjusted automatically.

## US equities: Alpaca IEX

`AlpacaIEXClient` uses the historical bars endpoint with `feed=iex` fixed in the
request and follows `next_page_token`. Provide `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY` in the environment. Credentials are request headers only;
they are not persisted. The IEX feed is one exchange and does not represent the
consolidated US market; Alpaca describes it as approximately 2.5% of US market
volume. Basic plan historical access is subject to account limits. This client
does not request SIP data or purchase subscriptions.

## Crypto: Binance public spot archives

`BinanceSpotArchiveClient` supports monthly SPOT one-minute kline archives and
SPOT aggregated-trade archive retrieval. It rejects non-spot API paths and
requires `terms_acknowledged=True`. It verifies the archive's SHA-256 sidecar
before parsing or returning bytes. Kline open-time values are interpreted as
milliseconds through 2024 and microseconds beginning January 2025. No account
credentials or live endpoints are used.

Binance's public-data repository terms specify CC BY-NC-SA 4.0, prohibit
commercial use without a separate license, and apply share-alike obligations to
adaptations. Review the current terms and obtain the required rights before
using or distributing data or derived datasets. This project does not redistribute
provider data.

## Validation, storage, and reproducibility

`assess_quality()` checks duplicate/order conditions, instrument identity,
exchange-session membership, scheduled gaps, observed zero-volume bars, large
close-to-close jumps, and unusual volume. Missing intervals remain missing;
zero-volume bars are retained as observed and are not treated as proof of no
trades. Jumps are labeled as possible corporate-action/dislocation events, not
as confirmed splits. No interpolation, corporate-action adjustment, or silent
sorting occurs.

`persist_dataset()` stores raw provider payloads, normalized 1-minute Parquet,
and deterministic 5- and 15-minute Parquet under a user data directory (by
default `~/.local/share/tradingagents/datasets`). Paths partition by market
calendar, instrument, year, month, interval, and transformation version.
Dataset IDs derive from provider, instrument, requested range, raw checksums,
and transformation version. Artifacts are written atomically and immutable;
different source bytes produce a distinct dataset ID. Each manifest records the
source, ranges, retrieval time, counts, gaps, checksums, metadata version,
calendar implementation/version, transformation version, terms reference, and
the limited-universe/corporate-action policy. Keep licensed raw data outside Git.

## Pilot and expansion

Resolve the pilot symbols against the daily official master, then request about
60 exchange sessions ending at an explicitly chosen date. The requested date
range should be recorded in the manifest. Once a pilot passes validation, extend
the requested range in month-sized chunks towards two years, subject to account
entitlement and available history. The code includes a session-count range
helper; it does not initiate downloads itself. No real dataset is bundled with
this source tree.

## Official references

- [Upstox Historical Candle V3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)
- [Upstox rate limits](https://upstox.com/developer/api-documentation/rate-limiting/)
- [Upstox instrument metadata](https://upstox.com/developer/api-documentation/instruments/)
- [Alpaca historical stock bars](https://docs.alpaca.markets/us/v1.1/docs/historical-stock-data-1)
- [Alpaca market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)
- [Binance public-data repository](https://github.com/binance/binance-public-data)
- [Binance public-data terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md)
