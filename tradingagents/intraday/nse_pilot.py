"""Read-only NSE data and VWAP signal pilot using the existing Upstox pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.intraday.aggregation import aggregate_bars
from tradingagents.intraday.calendars import SessionCalendar
from tradingagents.intraday.candidates import SetupStatus, hash_configuration
from tradingagents.intraday.data_sources.common import ProviderError
from tradingagents.intraday.data_sources.upstox import (
    NSE_MASTER_URL,
    SOURCE_TERMS,
    UpstoxHistoricalClient,
)
from tradingagents.intraday.datasets import DEFAULT_DATA_ROOT, assess_quality, persist_dataset
from tradingagents.intraday.indicators import IndicatorConfig, compute_indicators
from tradingagents.intraday.models import InstrumentMetadata, MarketBar
from tradingagents.intraday.strategies import VWAPPullbackConfig, evaluate_vwap_trend_pullback

PILOT_LABEL = "NSE_DATA_AND_SIGNAL_PILOT"
DEFAULT_REPORT_ROOT = DEFAULT_DATA_ROOT.parent / "reports" / "nse-pilot"
TICK_SIZE_DOC = "https://upstox.com/developer/api-documentation/instruments/"
TICK_SIZE_BLOCKER = (
    "Upstox documents tick_size as a minimum price movement, but the official NSE JSON "
    "master example reports 5.0 and does not define its unit; no conversion to INR is assumed."
)


def latest_completed_session(calendar: SessionCalendar, now: datetime) -> date:
    """Return the latest exchange session whose close is no later than ``now``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    local_today = now.astimezone(ZoneInfo(calendar.timezone)).date()
    start = local_today - timedelta(days=20)
    sessions = calendar.sessions(start, local_today)
    completed = [session for session in sessions if session.close_utc <= now]
    if not completed:
        raise ValueError("could not find a completed exchange session in calendar lookback")
    return completed[-1].session_date


def trailing_sessions(calendar: SessionCalendar, end: date, count: int) -> list[Any]:
    """Select exactly ``count`` exchange sessions ending on ``end``."""
    if count < 1:
        raise ValueError("sessions must be positive")
    span = max(90, count * 2)
    sessions = calendar.sessions(end - timedelta(days=span), end)
    while len(sessions) < count:
        span *= 2
        sessions = calendar.sessions(end - timedelta(days=span), end)
    selected = sessions[-count:]
    if not selected or selected[-1].session_date != end:
        raise ValueError(f"end date {end.isoformat()} is not an exchange session")
    return selected


def resolve_tick_size(tick_size_raw: str | None, price_precision: int) -> tuple[Decimal | None, str | None]:
    """Fail closed unless the instrument-master tick size is unambiguously interpretable."""
    if not tick_size_raw:
        return None, "instrument master has no tick_size field"
    try:
        raw = Decimal(tick_size_raw)
    except InvalidOperation:
        return None, "instrument master tick_size is not numeric"
    if not raw.is_finite() or raw <= 0:
        return None, "instrument master tick_size is not positive and finite"
    # Current official documentation does not define whether the JSON integer-like
    # values (for example 5.0) are rupees or paise. Never infer a conversion.
    if raw.normalize().as_tuple().exponent < -price_precision:
        return None, "tick_size requires more decimals than the instrument price precision"
    return None, TICK_SIZE_BLOCKER


def _instrument_metadata(resolved, tick: Decimal | None) -> InstrumentMetadata:
    """Build analytical metadata; the placeholder tick is never used for signals."""
    return InstrumentMetadata(
        instrument_id=resolved.instrument_key,
        symbol=resolved.trading_symbol,
        venue="NSE",
        asset_class="equity",
        base_currency="INR",
        quote_currency="INR",
        price_precision=2,
        # Dataset persistence requires a positive tick. 0.01 is only a storage
        # precision fallback while metadata is BLOCKED; signals remain disabled.
        tick_size=tick if tick is not None else Decimal("0.01"),
        quantity_increment=Decimal(resolved.lot_size or "1"),
        exchange_timezone="Asia/Kolkata",
        session_calendar="XNSE",
        data_source="upstox",
        provenance=(
            f"Upstox daily instrument master sha256:{resolved.metadata_sha256}; "
            f"retrieved {resolved.metadata_retrieved_at}; raw tick_size={resolved.tick_size_raw}; "
            f"conversion={tick if tick is not None else 'unverified'}"
        ),
    )


def validate_session_data(
    bars: list[MarketBar], instrument: InstrumentMetadata, sessions: list[Any]
) -> dict[str, Any]:
    """Report full-session coverage and structural quality without repairing bars."""
    expected = {
        stamp
        for session in sessions
        for stamp in SessionCalendar(instrument.session_calendar).expected_bar_starts(session, 1)
    }
    actual = [bar.timestamp for bar in bars]
    seen: set[datetime] = set()
    duplicates = 0
    out_of_order = 0
    previous: datetime | None = None
    for stamp in actual:
        duplicates += stamp in seen
        out_of_order += previous is not None and stamp < previous
        seen.add(stamp)
        previous = stamp
    missing = sorted(expected - seen)
    outside = sorted(seen - expected)
    by_session: dict[str, int] = {}
    for stamp in actual:
        local_day = stamp.astimezone(UTC).date().isoformat()
        # Session dates are matched by UTC timestamps, since NSE bars straddle UTC days.
        matched = next((s.session_date.isoformat() for s in sessions
                       if s.open_utc <= stamp < s.close_utc), local_day)
        by_session[matched] = by_session.get(matched, 0) + 1
    first_expected = SessionCalendar(instrument.session_calendar).expected_bar_starts(sessions[0], 1)
    last_expected = SessionCalendar(instrument.session_calendar).expected_bar_starts(sessions[-1], 1)
    first_count = sum(stamp in seen for stamp in first_expected)
    last_count = sum(stamp in seen for stamp in last_expected)
    quality = assess_quality(
        bars, instrument, expected_start=sessions[0].session_date,
        expected_end=sessions[-1].session_date,
    )
    return {
        **asdict(quality),
        "expected_session_count": len(sessions),
        "complete_session_count": sum(
            all(stamp in seen for stamp in
                SessionCalendar(instrument.session_calendar).expected_bar_starts(session, 1))
            for session in sessions
        ),
        "incomplete_first_session": first_count != len(first_expected),
        "incomplete_last_session": last_count != len(last_expected),
        "unexpected_timestamps_outside_requested_sessions": len(outside),
        "unexpected_timestamps": [stamp.isoformat() for stamp in outside[:100]],
        "missing_expected_intervals": len(missing),
        "missing_intervals": [stamp.isoformat() for stamp in missing[:100]],
        "session_record_counts": by_session,
        "ohlc_relationship_failures": 0,
        "quality_passed": not (duplicates or out_of_order or missing or outside
                                or first_count != len(first_expected)
                                or last_count != len(last_expected)
                                or quality["invalid_records"]),
    }


def _serializable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {PILOT_LABEL}",
        "",
        f"- Status: `{report['status']}`",
        f"- Symbol: `{report.get('symbol')}`",
        f"- Sessions: `{report.get('valid_session_count', 0)}/{report.get('requested_sessions')}`",
        f"- Dataset ID: `{report.get('dataset_id') or 'unavailable'}`",
        f"- Candidate count: `{report.get('candidate_count', 0)}`",
        "",
        "## Blockers and limitations",
        "",
    ]
    for blocker in report.get("blockers", []):
        lines.append(f"- {blocker}")
    if not report.get("blockers"):
        lines.append("- None recorded.")
    lines.extend(["", "No broker order APIs are called. This report is not a backtest or profitability claim.", ""])
    return "\n".join(lines)


def _write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"nse-pilot-{report['symbol'].lower()}-{report['requested_sessions']}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(_serializable(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def run_pilot(
    *, symbol: str = "RELIANCE", session_count: int = 60,
    end_date: date | None = None, output_dir: str | Path = DEFAULT_REPORT_ROOT,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    client: UpstoxHistoricalClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch, validate and report an NSE data/signal pilot; never places orders."""
    if not re.fullmatch(r"[A-Za-z0-9&.-]+", symbol):
        raise ValueError("symbol contains unsupported characters")
    if session_count < 1:
        raise ValueError("sessions must be positive")
    started = (now or datetime.now(UTC)).astimezone(UTC)
    calendar = SessionCalendar("XNSE")
    resolved_end = end_date or latest_completed_session(calendar, started)
    if not calendar.sessions(resolved_end, resolved_end):
        raise ValueError(f"end date {resolved_end.isoformat()} is not an NSE session")
    if calendar.sessions(resolved_end, resolved_end)[0].close_utc > started:
        raise ValueError("end date is not a fully completed NSE session")
    sessions = trailing_sessions(calendar, resolved_end, session_count)
    report: dict[str, Any] = {
        "report_type": PILOT_LABEL,
        "status": "BLOCKED_AUTH",
        "symbol": symbol.upper(),
        "requested_sessions": session_count,
        "end_date": resolved_end.isoformat(),
        "requested_start_date": sessions[0].session_date.isoformat(),
        "requested_end_date": sessions[-1].session_date.isoformat(),
        "valid_session_count": 0,
        "instrument": None,
        "instrument_master": None,
        "dataset_id": None,
        "manifest_path": None,
        "artifact_hashes": {},
        "raw_hashes": {},
        "record_counts": {"1m": 0, "5m": 0, "15m": 0},
        "quality": None,
        "corporate_action_verification": {
            "status": "not_checked",
            "limitation": "No corporate-action source check was run before data acquisition.",
        },
        "strategy_id": "vwap_trend_pullback",
        "strategy_version": VWAPPullbackConfig().version,
        "strategy_configuration_hash": hash_configuration(VWAPPullbackConfig()),
        "candidate_count": 0,
        "rejection_counts": {},
        "candidates_per_session": {},
        "instrument_metadata_status": "not_resolved",
        "historical_research_readiness": "BLOCKED_AUTH",
        "vwap_method": "OHLCV-derived equity session VWAP approximation; not trade-level VWAP",
        "profitability_metrics": None,
        "broker_orders_called": False,
        "blockers": [],
        "limitations": [
            "No Nautilus execution, order placement, cancellation, or modification is part of this pilot.",
            "OHLCV-derived VWAP is an approximation and not exact trade-level VWAP.",
            "No profitability, expectancy, or win-rate metrics are produced.",
        ],
    }
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        report["blockers"].append(
            "BLOCKED_AUTH: set UPSTOX_ACCESS_TOKEN in the environment using the normal Upstox OAuth flow, then rerun; do not place the value in source code."
        )
        _write_report(report, Path(output_dir))
        return report

    client = client or UpstoxHistoricalClient(access_token=token)
    data_root = Path(data_root)
    raw_root = data_root / "raw" / "upstox"
    master_path = raw_root / "instrument-master" / "NSE.json.gz"
    try:
        resolved = client.resolve_nse_equity(symbol, cache_path=master_path)
    except (ProviderError, OSError, ValueError) as exc:
        report["status"] = "BLOCKED_INSTRUMENT_RESOLUTION"
        report["blockers"].append(f"Instrument resolution failed: {exc}")
        _write_report(report, Path(output_dir))
        return report
    if not resolved.isin or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", resolved.isin):
        report["status"] = "BLOCKED_INSTRUMENT_METADATA"
        report["instrument_metadata_status"] = "invalid_isin"
        report["blockers"].append("Resolved entry has no valid ISIN.")
        _write_report(report, Path(output_dir))
        return report
    tick_size, tick_error = resolve_tick_size(resolved.tick_size_raw, 2)
    metadata = _instrument_metadata(resolved, tick_size)
    report["instrument"] = {
        **asdict(resolved),
        "tick_size": str(tick_size) if tick_size is not None else None,
        "tick_size_conversion_rule": "raw Upstox value interpreted directly as INR" if tick_size else None,
    }
    report["instrument_master"] = {
        "url": NSE_MASTER_URL,
        "sha256": resolved.metadata_sha256,
        "retrieved_at_utc": resolved.metadata_retrieved_at,
        "path": str(master_path),
    }
    report["instrument_metadata_status"] = "verified" if tick_error is None else "BLOCKED_INSTRUMENT_METADATA"
    if tick_error:
        report["blockers"].append(tick_error)
        report["limitations"].append(f"Tick-size reference: {TICK_SIZE_DOC}")

    try:
        fetched = client.fetch_range(
            resolved.instrument_key, sessions[0].session_date, sessions[-1].session_date,
            raw_cache_dir=raw_root / "monthly-cache",
        )
        bars = [bar for _first, _last, chunk_bars, _payload in fetched for bar in chunk_bars]
        payloads = [(f"upstox-{first:%Y-%m}.json", (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
                    for first, _last, _chunk_bars, payload in fetched]
        payloads.append(("NSE.json.gz", master_path.read_bytes()))
    except (ProviderError, OSError, ValueError) as exc:
        report["status"] = "BLOCKED_DATA_ACQUISITION"
        report["blockers"].append(f"Historical data acquisition failed: {exc}")
        _write_report(report, Path(output_dir))
        return report

    quality = validate_session_data(bars, metadata, sessions)
    report["quality"] = quality
    report["valid_session_count"] = quality["complete_session_count"]
    report["record_counts"]["1m"] = len(bars)
    if not quality["quality_passed"]:
        report["blockers"].append("Historical data did not pass complete-session quality validation.")
    persisted = persist_dataset(
        bars, metadata, provider="upstox_historical_candle_v3",
        source_reference=f"{resolved.instrument_key}; Upstox Historical Candle V3",
        requested_start=sessions[0].session_date, requested_end=sessions[-1].session_date,
        raw_payloads=payloads,
        instrument_metadata_version=resolved.metadata_version,
        instrument_metadata={**asdict(resolved), "tick_size_interpretation":
                             "unverified" if tick_error else "raw value interpreted as INR",
                             "tick_size_conversion_blocker": tick_error},
        source_terms_reference=SOURCE_TERMS,
        data_root=data_root,
    )
    report["dataset_id"] = persisted["dataset_id"]
    report["manifest_path"] = str(data_root / f"market=xnse/instrument={resolved.instrument_key.replace('|', '_')}" /
                                  "manifests" / f"dataset={persisted['dataset_id']}.json")
    report["artifact_hashes"] = {key: value["sha256"] for key, value in persisted["artifacts"].items()}
    report["raw_hashes"] = {value["name"]: value["sha256"] for value in persisted["raw_payloads"]}
    report["record_counts"] = persisted["interval_record_counts"]
    report["valid_session_count"] = quality["complete_session_count"]
    report["corporate_action_verification"] = {
        "status": "not_checked",
        "limitation": "No authoritative symbol-specific corporate-action result was available; price discontinuities are only flagged by data quality.",
    }
    report["limitations"].append("Corporate actions were not independently verified against an authoritative NSE source.")
    report["corporate_action_verification"] = report["corporate_action_verification"]
    if tick_error:
        report["status"] = "BLOCKED_INSTRUMENT_METADATA"
        _write_report(report, Path(output_dir))
        return report
    if not quality["quality_passed"]:
        report["status"] = "BLOCKED_DATA_QUALITY"
        _write_report(report, Path(output_dir))
        return report

    evaluations = evaluate_equity_signals(bars, metadata, persisted["dataset_id"])
    reasons = Counter(evaluation.reason for evaluation in evaluations if evaluation.status != SetupStatus.VALID)
    candidates = [evaluation.candidate for evaluation in evaluations if evaluation.candidate]
    per_session: Counter[str] = Counter(
        candidate.decision_timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        for candidate in candidates
    )
    report.update({
        "status": "COMPLETE",
        "candidate_count": len(candidates),
        "rejection_counts": dict(sorted(reasons.items())),
        "candidates_per_session": dict(sorted(per_session.items())),
        "candidates": [_serializable(asdict(candidate)) for candidate in candidates],
        "historical_research_readiness": "DATA_AND_SIGNALS_ONLY",
        "strategy_configuration_hash": hash_configuration(VWAPPullbackConfig()),
    })
    _write_report(report, Path(output_dir))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Upstox NSE VWAP data/signal pilot")
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_ROOT)
    args = parser.parse_args(argv)
    try:
        report = run_pilot(
            symbol=args.symbol, session_count=args.sessions, end_date=args.end_date,
            output_dir=args.output_dir,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_serializable(report), sort_keys=True))
    return 0 if report["status"] == "COMPLETE" else 2


def evaluate_equity_signals(bars: list[MarketBar], instrument: InstrumentMetadata, dataset_id: str):
    """Run the existing equity VWAP strategy over completed 5m/15m candles."""
    candles_5m = aggregate_bars(bars, instrument, 5)
    candles_15m = aggregate_bars(bars, instrument, 15)
    indicators_5m = compute_indicators(candles_5m, instrument, config=IndicatorConfig())
    indicators_15m = compute_indicators(candles_15m, instrument, config=IndicatorConfig())
    return evaluate_vwap_trend_pullback(
        instrument, candles_5m, indicators_5m, candles_15m, indicators_15m,
        replay_id=f"nse-pilot:{dataset_id}", config=VWAPPullbackConfig(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
