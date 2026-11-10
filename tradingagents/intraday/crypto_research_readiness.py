"""Deterministic BTCUSDT research readiness command; never runs historical data."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from tradingagents.intraday.crypto_exchange import (
    BinanceSpotFilters,
    parse_binance_spot_exchange_info,
)
from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotExecutionSpec,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.crypto_experiment import CryptoExperimentMode, run_crypto_experiment
from tradingagents.intraday.crypto_research import (
    CryptoResearchProfile,
    canonical_hash,
    source_hashes,
)
from tradingagents.intraday.experiment_validation import (
    DatasetVerificationError,
    VerifiedDataset,
    verify_dataset_manifest,
)
from tradingagents.intraday.models import InstrumentMetadata, MarketBar
from tradingagents.intraday.strategies import (
    CRYPTO_VWAP_PULLBACK_ID,
    CryptoVWAPPullbackConfig,
    OpeningRangeConfig,
)

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(__file__).resolve().parent / "data" / "binance"
EXPECTED_DATASET_ID = "819f13eb67b47c7a01634c10"
EXPECTED_COUNTS = {"1m": 44_640, "5m": 8_928, "15m": 2_976}
EXPECTED_RANGE = ("2025-01-01T00:00:00+00:00", "2025-01-31T23:59:00+00:00")


def _json_default(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _instrument(filters: BinanceSpotFilters) -> InstrumentMetadata:
    if (filters.symbol, filters.base_asset, filters.quote_asset) != ("BTCUSDT", "BTC", "USDT"):
        raise ValueError("filter snapshot identity is not BTCUSDT/BTC/USDT")
    return InstrumentMetadata(
        "BTCUSDT.BINANCE_SPOT",
        "BTCUSDT",
        "BINANCE_SPOT",
        "crypto_spot",
        "BTC",
        "USDT",
        filters.base_asset_precision,
        filters.tick_size,
        filters.lot_step_size,
        "UTC",
        "24/7",
        "BINANCE_SPOT:BTCUSDT",
        "verified BTCUSDT current filter snapshot",
        filters.lot_min_quantity,
        filters.min_notional,
    )


def _read_filter_snapshot(path: str | Path) -> tuple[BinanceSpotFilters, str, str]:
    record_path = Path(path)
    try:
        wrapper = json.loads(record_path.read_text(encoding="utf-8"))
        if wrapper.get("snapshot_kind") != "current_exchange_rules_not_historical_rules":
            raise ValueError("FILTER_SNAPSHOT_NOT_CURRENT_ONLY")
        raw_path = Path(wrapper["raw_response_path"])
        raw = raw_path.read_bytes()
        raw_hash = hashlib.sha256(raw).hexdigest()
        stored = wrapper["filters"]
        retrieved = datetime.fromisoformat(stored["retrieved_at_utc"])
        filters = parse_binance_spot_exchange_info(raw, retrieved_at=retrieved)
        if filters.raw_response_sha256 != raw_hash or stored.get("raw_response_sha256") != raw_hash:
            raise ValueError("FILTER_SNAPSHOT_HASH_MISMATCH")
        if filters.as_record() != stored:
            raise ValueError("FILTER_SNAPSHOT_NORMALIZED_RECORD_MISMATCH")
        return filters, raw_hash, retrieved.isoformat()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _verify_dataset(manifest_path: str | Path, instrument: InstrumentMetadata) -> VerifiedDataset:
    # The retained Phase 4B manifest names the explicit BINANCE_SPOT:BTCUSDT alias.
    from dataclasses import replace

    alias = replace(instrument, instrument_id="BINANCE_SPOT:BTCUSDT")
    return verify_dataset_manifest(
        manifest_path,
        alias,
        expected_dataset_id=EXPECTED_DATASET_ID,
        expected_counts=EXPECTED_COUNTS,
        expected_actual_range_utc=EXPECTED_RANGE,
    )


def _synthetic_manifest(directory: Path, canonical: InstrumentMetadata) -> Path:
    """Create a clearly synthetic fixture for the real strategy-to-Nautilus self-check."""
    from dataclasses import replace

    from tradingagents.intraday.datasets import persist_dataset

    alias = replace(canonical, instrument_id="BINANCE_SPOT:BTCUSDT")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars = []
    for day in range(2):
        previous = D("100")
        for group in range(288):
            close = D("100") + D("0.001") * group
            if group >= 201:
                close += D("0.02")
            if group >= 241:
                close += D("0.02")
            open_ = previous
            high = max(open_, close) + D("0.01")
            low = min(open_, close) - D("0.10")
            if group == 200:
                low = min(D("100.20"), open_, close)
            volume = D("4") if group == 201 else D("2")
            for minute in range(5):
                stamp = start + timedelta(days=day, minutes=group * 5 + minute)
                minute_high = max(high, D("100.30")) if group == 202 and minute == 1 else high
                bars.append(
                    MarketBar(
                        stamp,
                        alias.instrument_id,
                        open_ if minute == 0 else close,
                        minute_high,
                        low,
                        close,
                        volume,
                        f"synthetic-ready-{day}-{group:03}-{minute}",
                        "SYNTHETIC readiness self-check fixture",
                        1,
                    )
                )
            previous = close
    persisted = persist_dataset(
        bars,
        alias,
        provider="synthetic",
        source_reference="SYNTHETIC readiness self-check",
        requested_start=start.date(),
        requested_end=(start + timedelta(days=1)).date(),
        raw_payloads=[("synthetic-readiness.csv", b"SYNTHETIC; generated locally; no market data")],
        instrument_metadata_version="synthetic-readiness-v1",
        source_terms_reference="SYNTHETIC; not market data",
        data_root=directory / "dataset",
    )
    return (
        directory
        / "dataset"
        / "market=24_7"
        / "instrument=BINANCE_SPOT_BTCUSDT"
        / "manifests"
        / f"dataset={persisted['dataset_id']}.json"
    )


def _run_synthetic_self_check(instrument: InstrumentMetadata, filters: BinanceSpotFilters) -> dict:
    with tempfile.TemporaryDirectory(prefix="crypto-readiness-synthetic-") as temporary:
        base = Path(temporary)
        manifest = _synthetic_manifest(base, instrument)
        spec = CryptoSpotExecutionSpec(
            instrument,
            "USDT",
            filters.base_asset_precision,
            "USDT",
            "synthetic-readiness-fees-v1",
            "synthetic software self-check",
            filters.retrieved_at_utc,
            filters,
        )
        account = CryptoSpotAccountConfig(
            "synthetic-readiness-account",
            "USDT",
            D("10000"),
            D("0"),
            None,
            D("10"),
            D("100"),
            1,
            4,
            1440,
            "UTC",
            5,
        )
        fees = CryptoSpotFeeConfiguration(
            D("0.0008"),
            D("0.001"),
            "USDT",
            "SYNTHETIC test assumption",
            filters.retrieved_at_utc,
            False,
            "synthetic-readiness-fees-v1",
        )
        costs = CryptoSpotCostAssumptions(
            fees.taker_model(),
            D("1"),
            D("2"),
            "SYNTHETIC test assumptions",
            False,
            "synthetic-readiness-costs-v1",
        )
        result = run_crypto_experiment(
            manifest,
            instrument,
            mode=CryptoExperimentMode.SYNTHETIC_SELF_CHECK,
            output_directory=base / "reports",
            execution_spec=spec,
            account=account,
            fee_configuration=fees,
            costs=costs,
        )
        fills = result.get("fills", [])
        events = result.get("worker_events", [])
        approvals = {
            event.get("candidate_id"): event["timestamp"]
            for event in events
            if event.get("type") == "risk_approved"
        }
        submissions = [event for event in events if event.get("type") == "order_submit_intent"]
        return {
            "executed": True,
            "synthetic_only": result.get("synthetic_label") == "SYNTHETIC",
            "status": result.get("status"),
            "reason": result.get("blocked_reason"),
            "candidate_count": result.get("candidate_count", 0),
            "risk_approval_count": result.get("risk_approval_count", 0),
            "risk_precedes_submission": bool(submissions)
            and all(
                approvals.get(item.get("candidate_id")) is not None
                and approvals[item["candidate_id"]] <= item["timestamp"]
                for item in submissions
            ),
            "fill_count": len(fills),
            "ledger_reconciliation_passed": result.get("ledger_reconciliation", {}).get("passed")
            is True,
            "cost_adjusted_fills": bool(fills)
            and all(
                D(fill.get("adjusted_price", "0")) == D(fill.get("price", "-1"))
                and D(fill.get("reference_price", "0")) != D(fill["price"])
                for fill in fills
            ),
            "fill_fee_classifications": sorted({fill.get("fee_classification") for fill in fills}),
            "account_type": result.get("account_type"),
            "account_currency": result.get("account_currency"),
            "borrowing_enabled": result.get("account_authority", {}).get("borrowing_enabled"),
            "fee_currencies": sorted({fill.get("fee_currency") for fill in fills}),
            "historical_performance_metrics": result.get("historical_pnl_metrics"),
            "future_candidate_event_gate_passed": all(
                event["timestamp"] >= schedule["decision_timestamp"]
                for schedule in result.get("candidate_schedule", [])
                for event in result.get("worker_events", [])
                if event.get("candidate_id") == schedule["candidate_id"]
            ),
        }


def _blocker(code: str, category: str, detail: str) -> dict[str, str]:
    return {"code": code, "category": category, "detail": detail}


def build_crypto_research_readiness(
    *,
    manifest_path: str | Path,
    config_path: str | Path,
    filter_record_path: str | Path = DATA_DIR / "BTCUSDT-exchangeInfo-normalized.json",
) -> dict:
    """Verify configuration, data, pinned integration, and run synthetic execution only."""
    blockers: list[dict[str, str]] = []
    try:
        profile = CryptoResearchProfile.load_yaml(config_path)
    except ValueError as exc:
        profile = None
        blockers.append(_blocker("RESEARCH_CONFIG_INVALID", "configuration", str(exc)))

    if profile:
        blockers.extend(
            _blocker(code, "configuration", "required research input is unset or unsupported")
            for code in profile.blockers()
        )
        if (
            profile.spread_bps is None
            or profile.slippage_bps is None
            or profile.taker_fee_bps is None
        ):
            blockers.append(
                _blocker(
                    "EXECUTION_COST_MODEL_INCOMPLETE",
                    "configuration",
                    "taker fee, spread, and slippage must all be explicitly configured",
                )
            )
    filters = None
    raw_filter_hash = None
    filter_timestamp = None
    filter_error = None
    try:
        filters, raw_filter_hash, filter_timestamp = _read_filter_snapshot(filter_record_path)
    except ValueError as exc:
        filter_error = str(exc)
        code = (
            "FILTER_SNAPSHOT_HASH_MISMATCH"
            if "HASH_MISMATCH" in filter_error or "NORMALIZED_RECORD_MISMATCH" in filter_error
            else "FILTER_SNAPSHOT_MISSING"
        )
        blockers.append(_blocker(code, "data", filter_error))

    instrument = (
        _instrument(filters)
        if filters
        else InstrumentMetadata(
            "BTCUSDT.BINANCE_SPOT",
            "BTCUSDT",
            "BINANCE_SPOT",
            "crypto_spot",
            "BTC",
            "USDT",
            2,
            D("0.01"),
            D("0.00001"),
            "UTC",
            "24/7",
            "BINANCE_SPOT:BTCUSDT",
            "identity-only fallback while current filter snapshot is blocked",
            D("0.00001"),
            D("5"),
        )
    )
    dataset = None
    dataset_error = None
    try:
        dataset = _verify_dataset(manifest_path, instrument)
    except (DatasetVerificationError, OSError, ValueError) as exc:
        dataset_error = str(exc)
        if not Path(manifest_path).is_file():
            code = "DATASET_MANIFEST_MISSING"
        elif "checksum" in dataset_error.lower() or "hash" in dataset_error.lower():
            code = "DATASET_CHECKSUM_FAILED"
        else:
            code = "DATASET_VERIFICATION_FAILED"
        blockers.append(_blocker(code, "data", dataset_error))

    synthetic_check = {"executed": False, "status": "BLOCKED_INTEGRATION"}
    if filters is not None:
        blockers.append(
            _blocker(
                "HISTORICAL_FILTER_COMPATIBILITY_UNVERIFIED",
                "configuration",
                "retained Binance filters are current-only and do not establish January 2025 rules",
            )
        )
        try:
            synthetic_check = _run_synthetic_self_check(instrument, filters)
            if (
                synthetic_check["status"] != "COMPLETE"
                or not synthetic_check["ledger_reconciliation_passed"]
                or not synthetic_check["cost_adjusted_fills"]
                or not synthetic_check["synthetic_only"]
            ):
                blockers.append(
                    _blocker(
                        "LEDGER_RECONCILIATION_FAILED",
                        "integration",
                        "synthetic Nautilus/cost/ledger self-check did not pass",
                    )
                )
        except Exception as exc:
            synthetic_check = {"executed": True, "status": "FAILED", "error": str(exc)}
            blockers.append(_blocker("SYNTHETIC_INTEGRATION_FAILED", "integration", str(exc)))
    else:
        blockers.append(
            _blocker(
                "SYNTHETIC_INTEGRATION_FAILED",
                "integration",
                "cannot run synthetic Nautilus check without a verified filter snapshot",
            )
        )

    version_check = subprocess.run(
        [
            str(ROOT / ".venv-nautilus" / "bin" / "python"),
            "-c",
            "import nautilus_trader; print(nautilus_trader.__version__)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    nautilus_version = version_check.stdout.strip() if version_check.returncode == 0 else None
    if nautilus_version != "1.231.0":
        blockers.append(
            _blocker("NAUTILUS_VERSION_INCOMPATIBLE", "integration", str(nautilus_version))
        )

    strategy_config = CryptoVWAPPullbackConfig()
    strategy_checks = {
        "strategy_id": CRYPTO_VWAP_PULLBACK_ID,
        "strategy_version": strategy_config.version,
        "utc_vwap_reset": strategy_config.utc_anchor.isoformat(),
        "vwap_price_input": strategy_config.price_input,
        "vwap_volume_input": strategy_config.volume_input,
        "completed_candle_semantics": strategy_config.candle_completion,
        "crypto_orb_disabled_by_default": OpeningRangeConfig().allow_crypto is False,
        "no_lookahead_synthetic_replay": synthetic_check.get(
            "future_candidate_event_gate_passed", False
        ),
        "nautilus_version": nautilus_version,
        "cash_account_and_currency": (
            instrument.quote_currency == "USDT"
            and instrument.base_currency == "BTC"
            and synthetic_check.get("account_currency") == "USDT"
        ),
        "borrowing_disabled": synthetic_check.get("borrowing_enabled") is False,
        "risk_approval_before_submission": synthetic_check.get("risk_approval_count", 0) > 0,
        "strategy_candidates_generated": synthetic_check.get("candidate_count", 0) > 0,
        "risk_precedes_order_submission": synthetic_check.get("risk_precedes_submission", False),
        "fee_model_available": synthetic_check.get("fill_fee_classifications") == ["TAKER"],
        "usdt_fee_currency": synthetic_check.get("fee_currencies") in ([], ["USDT"]),
        "cash_account": synthetic_check.get("account_type") == "CASH",
        "execution_price_adjustment_configured": synthetic_check.get("cost_adjusted_fills", False),
        "ledger_reconciliation_passed": synthetic_check.get("ledger_reconciliation_passed", False),
    }
    descriptive = {
        "strategy_id",
        "strategy_version",
        "utc_vwap_reset",
        "vwap_price_input",
        "vwap_volume_input",
        "completed_candle_semantics",
        "nautilus_version",
    }
    if not all(
        value is not False for key, value in strategy_checks.items() if key not in descriptive
    ):
        blockers.append(
            _blocker(
                "STRATEGY_OR_EXECUTION_CHECK_FAILED",
                "integration",
                "one or more strategy/integration invariants failed",
            )
        )

    source_paths = tuple(
        Path(__file__).resolve().parent / name
        for name in (
            "crypto_costs.py",
            "crypto_execution.py",
            "crypto_exchange.py",
            "crypto_nautilus.py",
            "crypto_experiment.py",
            "crypto_pipeline.py",
            "aggregation.py",
            "indicators.py",
            "strategies.py",
            "candidates.py",
            "experiment_validation.py",
            "crypto_research.py",
            "crypto_research_readiness.py",
            "crypto_readiness.py",
            "nautilus_v1.py",
        )
    )
    source_digest = source_hashes(source_paths)
    snapshot = {
        "dataset": asdict(dataset) if dataset else None,
        "instrument": asdict(instrument),
        "filter_snapshot": {
            "sha256": raw_filter_hash,
            "retrieved_at_utc": filter_timestamp,
            "classification": "CURRENT_ONLY_NOT_JANUARY_2025_RULES",
            "normalized": filters.as_record() if filters else None,
        },
        "strategy": asdict(strategy_config),
        "account_and_cost_profile": profile.snapshot() if profile else None,
        "cost_sensitivity_scenarios": _scenario_snapshot(profile),
        "cost_model_version": "crypto-bps-cost-model-v1",
        "nautilus_version": nautilus_version,
        "source_sha256": source_digest,
    }
    experiment_hash = canonical_hash(snapshot)
    categories = {item["category"] for item in blockers}
    if "data" in categories:
        status = "BLOCKED_DATA"
    elif "integration" in categories:
        status = "BLOCKED_INTEGRATION"
    elif "configuration" in categories:
        status = "BLOCKED_CONFIGURATION"
    else:
        status = "READY_FOR_RESEARCH_BACKTEST"
    integration_valid = (
        strategy_checks["ledger_reconciliation_passed"]
        and strategy_checks["execution_price_adjustment_configured"]
        and strategy_checks["no_lookahead_synthetic_replay"]
        and strategy_checks["nautilus_version"] == "1.231.0"
    )
    ready = status == "READY_FOR_RESEARCH_BACKTEST"
    return {
        "report_version": "btcusdt-research-readiness-v1",
        "status": status,
        "experiment_configuration_hash": experiment_hash,
        "configuration_snapshot": snapshot,
        "configuration_hash_behavior": "canonical sorted JSON; Decimal values encoded as strings",
        "configuration_blockers": profile.blockers() if profile else ["RESEARCH_CONFIG_INVALID"],
        "blockers": blockers,
        "ready_for_historical_backtest": ready,
        "execution_cost_model_validated": strategy_checks["execution_price_adjustment_configured"],
        "nautilus_integration_validated": integration_valid,
        "ledger_reconciliation_passed": strategy_checks["ledger_reconciliation_passed"],
        "dataset": asdict(dataset) if dataset else None,
        "dataset_error": dataset_error,
        "filter_snapshot": {
            "path": str(Path(filter_record_path).resolve()),
            "sha256": raw_filter_hash,
            "retrieved_at_utc": filter_timestamp,
            "classification": "CURRENT_ONLY_NOT_JANUARY_2025_RULES",
            "error": filter_error,
        },
        "strategy_checks": strategy_checks,
        "synthetic_integration_self_check": synthetic_check,
        "historical_backtest_executed": False,
        "historical_cost_sensitivity_executed": False,
        "historical_performance_metrics": None,
        "research_edge_or_live_suitability_claimed": False,
    }


def _scenario_snapshot(profile: CryptoResearchProfile | None) -> list[dict]:
    if profile is None:
        return []
    from tradingagents.intraday.crypto_research import cost_sensitivity_scenarios

    return [asdict(item) for item in cost_sensitivity_scenarios(profile)]


def render_summary(report: dict) -> str:
    lines = [
        "# BTCUSDT research readiness",
        "",
        f"Status: **{report['status']}**",
        f"Experiment configuration hash: `{report['experiment_configuration_hash']}`",
        "",
        "The deterministic synthetic Nautilus integration check is software validation only. No historical performance is reported.",
        "",
        "## Blockers",
        "",
    ]
    if report["blockers"]:
        lines.extend(
            f"- `{item['code']}` ({item['category']}): {item['detail']}"
            for item in report["blockers"]
        )
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Dataset and filter",
            "",
            f"- Dataset: {'verified' if report['dataset'] else 'blocked'}",
            f"- Current filter snapshot SHA-256: `{report['filter_snapshot']['sha256']}`",
            f"- Filter retrieved at: `{report['filter_snapshot']['retrieved_at_utc']}`",
            "- Filter scope: current exchange rules only; not evidence of January 2025 rules.",
            "",
            "## Synthetic self-check",
            "",
            f"- Executed: `{report['synthetic_integration_self_check'].get('executed')}`",
            f"- Status: `{report['synthetic_integration_self_check'].get('status')}`",
            f"- Fills: `{report['synthetic_integration_self_check'].get('fill_count', 0)}`",
            f"- Ledger reconciliation: `{report['synthetic_integration_self_check'].get('ledger_reconciliation_passed')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", required=True, help="explicit January 2025 dataset manifest path"
    )
    parser.add_argument("--config", required=True, help="BTCUSDT research YAML profile")
    parser.add_argument("--filters", default=str(DATA_DIR / "BTCUSDT-exchangeInfo-normalized.json"))
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args(argv)
    report = build_crypto_research_readiness(
        manifest_path=args.manifest,
        config_path=args.config,
        filter_record_path=args.filters,
    )
    encoded = json.dumps(report, sort_keys=True, indent=2, default=_json_default) + "\n"
    summary = render_summary(report)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(encoded, encoding="utf-8")
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(summary, encoding="utf-8")
    print(encoded, end="")
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
