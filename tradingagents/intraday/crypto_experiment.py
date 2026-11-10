"""Fail-closed orchestration for BTCUSDT crypto simulation experiments."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from tradingagents.intraday.candidates import CandidateStatus, hash_configuration
from tradingagents.intraday.crypto_exchange import canonicalize_binance_spot_data
from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotExecutionSpec,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.crypto_nautilus import run_crypto_nautilus_execution
from tradingagents.intraday.crypto_pipeline import CryptoCandidateBatch, evaluate_crypto_market_data
from tradingagents.intraday.experiment_validation import (
    DatasetVerificationError,
    VerifiedDataset,
    one_minute_path,
    verify_dataset_manifest,
)
from tradingagents.intraday.ingest import file_sha256, read_market_data
from tradingagents.intraday.models import InstrumentMetadata
from tradingagents.intraday.strategies import CRYPTO_VWAP_PULLBACK_ID, CryptoVWAPPullbackConfig


class CryptoExperimentMode(StrEnum):
    SYNTHETIC_SELF_CHECK = "SYNTHETIC_SELF_CHECK"
    HISTORICAL = "HISTORICAL"


RUNNER_VERSION = "crypto-experiment-runner-v1"
EXECUTION_MODEL_ID = "nautilus-1.231.0-crypto-cash-bar-simulation"
FILL_MODEL_ID = "nautilus-1.231.0-crypto-costed-book-v1"
_HISTORICAL_CHECKS = (
    "ready_for_historical_backtest",
    "execution_cost_model_validated",
    "nautilus_integration_validated",
    "ledger_reconciliation_passed",
)


def run_crypto_experiment(
    manifest_path: str | Path,
    instrument: InstrumentMetadata,
    *,
    mode: CryptoExperimentMode | str,
    output_directory: str | Path,
    execution_spec: CryptoSpotExecutionSpec | None = None,
    account: CryptoSpotAccountConfig | None = None,
    fee_configuration: CryptoSpotFeeConfiguration | None = None,
    costs: CryptoSpotCostAssumptions | None = None,
    strategy_config: CryptoVWAPPullbackConfig | None = None,
    historical_readiness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify, load, evaluate, schedule and simulate one explicit dataset.

    Failed dataset preflight and historical-readiness checks are returned as
    persisted, deterministic blocked reports. Synthetic mode requires a
    manifest whose provider is explicitly ``synthetic``.
    """
    try:
        selected_mode = CryptoExperimentMode(mode)
    except ValueError as exc:
        raise ValueError(f"unsupported crypto experiment mode: {mode}") from exc

    config = strategy_config or CryptoVWAPPullbackConfig()
    config_hash = hash_configuration(config)
    manifest = Path(manifest_path)
    output_root = Path(output_directory)

    try:
        verified, one_minute_bars, manifest_record, manifest_checksum = _verify_and_load(
            manifest, instrument
        )
    except (DatasetVerificationError, OSError, ValueError, KeyError, TypeError) as exc:
        return _persist_result(
            _base_report(
                selected_mode,
                instrument,
                config,
                config_hash,
                manifest,
                None,
                None,
                status="BLOCKED_DATA",
                reason=str(exc),
            ),
            output_root,
        )

    if selected_mode == CryptoExperimentMode.SYNTHETIC_SELF_CHECK:
        if manifest_record.get("provider", "").lower() != "synthetic":
            return _persist_result(
                _base_report(
                    selected_mode,
                    instrument,
                    config,
                    config_hash,
                    manifest,
                    verified,
                    manifest_checksum,
                    status="BLOCKED_DATA",
                    reason="SYNTHETIC_SELF_CHECK requires a synthetic dataset manifest",
                ),
                output_root,
            )
    else:
        if manifest_record.get("provider", "").lower() == "synthetic":
            return _persist_result(
                _base_report(
                    selected_mode,
                    instrument,
                    config,
                    config_hash,
                    manifest,
                    verified,
                    manifest_checksum,
                    status="BLOCKED_READINESS",
                    reason="HISTORICAL mode refuses synthetic datasets",
                ),
                output_root,
            )
        blockers = _historical_blockers(
            instrument,
            execution_spec,
            account,
            fee_configuration,
            costs,
            historical_readiness,
        )
        if blockers:
            return _persist_result(
                _base_report(
                    selected_mode,
                    instrument,
                    config,
                    config_hash,
                    manifest,
                    verified,
                    manifest_checksum,
                    status="BLOCKED_READINESS",
                    reason="; ".join(blockers),
                    readiness_status="BLOCKED: " + "; ".join(blockers),
                ),
                output_root,
            )

    # `read_market_data` preserves source order; reject gaps rather than repair them.
    if any(bar.quality != "observed" for bar in one_minute_bars):
        return _persist_result(
            _base_report(
                selected_mode,
                instrument,
                config,
                config_hash,
                manifest,
                verified,
                manifest_checksum,
                status="BLOCKED_DATA",
                reason="dataset includes non-observed one-minute bars",
            ),
            output_root,
        )

    replay_id = hashlib.sha256(
        f"{verified.dataset_id}:{manifest_checksum}:{config_hash}".encode()
    ).hexdigest()
    batch: CryptoCandidateBatch = evaluate_crypto_market_data(
        instrument, one_minute_bars, replay_id=replay_id, config=config
    )
    candidates, duplicate_count = _unique_candidates(batch.candidates)

    # Candidate decisions are event-time scheduled: the worker dispatches a
    # candidate only on the event matching its completed decision timestamp.
    # It submits its entry on a strictly later one-minute event.
    if execution_spec is None or account is None or fee_configuration is None or costs is None:
        return _persist_result(
            _base_report(
                selected_mode,
                instrument,
                config,
                config_hash,
                manifest,
                verified,
                manifest_checksum,
                status="BLOCKED_READINESS",
                reason="execution spec, account, fee configuration, and costs are required",
            ),
            output_root,
        )

    run_id = _run_id(selected_mode, verified, manifest_checksum, config_hash)
    try:
        simulation = run_crypto_nautilus_execution(
            instrument,
            one_minute_bars,
            candidates,
            execution_spec=execution_spec,
            account=account,
            fee_configuration=fee_configuration,
            costs=costs,
            run_id=run_id,
        )
    except RuntimeError as exc:
        status = (
            "FAILED_RECONCILIATION" if "reconciliation" in str(exc).lower() else "FAILED_EXECUTION"
        )
        return _persist_result(
            _base_report(
                selected_mode,
                instrument,
                config,
                config_hash,
                manifest,
                verified,
                manifest_checksum,
                status=status,
                reason=str(exc),
                run_id=run_id,
                candidate_count=len(candidates),
                candidate_rejection_counts=_candidate_rejections(batch),
                duplicate_candidate_count=duplicate_count,
            ),
            output_root,
        )

    report = _report_from_simulation(
        selected_mode,
        instrument,
        config,
        config_hash,
        verified,
        manifest_checksum,
        account,
        fee_configuration,
        costs,
        batch,
        candidates,
        duplicate_count,
        simulation,
        run_id,
    )
    if not simulation.get("reconciliation", {}).get("passed", False):
        report["status"] = "FAILED_RECONCILIATION"
        report["readiness_status"] = "FAILED: ledger reconciliation did not pass"
    return _persist_result(report, output_root)


def _verify_and_load(
    manifest_path: Path, instrument: InstrumentMetadata
) -> tuple[VerifiedDataset, tuple, dict, str]:
    manifest_record = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_btcusdt_instrument(instrument)
    manifest_instrument_id = manifest_record.get("instrument_id")
    if manifest_instrument_id == "BINANCE_SPOT:BTCUSDT":
        manifest_instrument = instrument.__class__(
            **{
                **{name: getattr(instrument, name) for name in instrument.__dataclass_fields__},
                "instrument_id": manifest_instrument_id,
                "provenance": instrument.provenance + "; Phase 4B manifest identity",
            }
        )
    elif manifest_instrument_id == instrument.instrument_id:
        manifest_instrument = instrument
    else:
        raise DatasetVerificationError(
            "manifest instrument is not the canonical BTCUSDT Spot identity"
        )
    verified = verify_dataset_manifest(manifest_path, manifest_instrument)
    checksum = file_sha256(manifest_path)
    artifact_path = one_minute_path(manifest_record["artifacts"])
    ingestion = read_market_data(artifact_path, manifest_instrument)
    if ingestion.gaps:
        raise DatasetVerificationError(
            f"one-minute artifact contains {len(ingestion.gaps)} missing intervals"
        )
    if manifest_instrument.instrument_id == "BINANCE_SPOT:BTCUSDT":
        canonical_instrument, bars = canonicalize_binance_spot_data(
            manifest_instrument, ingestion.bars
        )
        if canonical_instrument.instrument_id != instrument.instrument_id:
            raise DatasetVerificationError(
                "BTCUSDT identity canonicalization produced an unexpected ID"
            )
    else:
        bars = ingestion.bars
    if not bars or any(bar.instrument_id != instrument.instrument_id for bar in bars):
        raise DatasetVerificationError("loaded bars do not match the selected BTCUSDT instrument")
    timestamps = tuple(bar.timestamp for bar in bars)
    if timestamps != tuple(sorted(timestamps)) or len(set(timestamps)) != len(timestamps):
        raise DatasetVerificationError("one-minute timestamps are duplicate or out of order")
    if any(bar.base_interval_minutes != 1 for bar in bars):
        raise DatasetVerificationError("execution source must contain one-minute bars")
    return verified, bars, manifest_record, checksum


def _validate_btcusdt_instrument(instrument: InstrumentMetadata) -> None:
    expected = {
        "instrument_id": "BTCUSDT.BINANCE_SPOT",
        "symbol": "BTCUSDT",
        "venue": "BINANCE_SPOT",
        "asset_class": "crypto_spot",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "exchange_timezone": "UTC",
        "session_calendar": "24/7",
    }
    mismatches = [key for key, value in expected.items() if getattr(instrument, key) != value]
    if mismatches:
        raise DatasetVerificationError(
            "selected instrument is not canonical BTCUSDT Spot: " + ", ".join(mismatches)
        )


def _historical_blockers(
    instrument: InstrumentMetadata,
    execution_spec: CryptoSpotExecutionSpec | None,
    account: CryptoSpotAccountConfig | None,
    fees: CryptoSpotFeeConfiguration | None,
    costs: CryptoSpotCostAssumptions | None,
    readiness: Mapping[str, Any] | None,
) -> list[str]:
    blockers: list[str] = []
    if execution_spec is None or execution_spec.exchange_filters is None:
        blockers.append("validated current instrument filters are unavailable")
    if account is None or account.account_currency != instrument.quote_currency:
        blockers.append("crypto account/risk configuration is incomplete")
    if (
        fees is None or fees.fee_currency != instrument.quote_currency
        or not fees.source.strip() or not fees.version.strip()
    ):
        blockers.append("explicit quote-currency fee configuration is incomplete")
    if costs is None or not costs.available or not costs.source or not costs.source.strip():
        blockers.append("explicit spread and slippage assumptions are incomplete")
    if readiness is None:
        blockers.append("validated historical readiness report is required")
    else:
        for key in _HISTORICAL_CHECKS:
            if readiness.get(key) is not True:
                blockers.append(f"historical readiness check {key} is not true")
    return blockers


def _unique_candidates(candidates: tuple) -> tuple[tuple, int]:
    seen: set[str] = set()
    unique = []
    duplicates = 0
    for candidate in sorted(
        candidates, key=lambda item: (item.decision_timestamp, item.candidate_id)
    ):
        if candidate.status != CandidateStatus.VALID:
            continue
        if candidate.candidate_id in seen:
            duplicates += 1
            continue
        seen.add(candidate.candidate_id)
        unique.append(candidate)
    return tuple(unique), duplicates


def _candidate_rejections(batch: CryptoCandidateBatch) -> dict[str, int]:
    reasons = Counter(
        evaluation.reason
        for evaluation in batch.evaluations
        if evaluation.candidate is None or evaluation.candidate.status != CandidateStatus.VALID
    )
    return dict(sorted(reasons.items()))


def _run_id(
    mode: CryptoExperimentMode, dataset: VerifiedDataset, manifest_checksum: str, config_hash: str
) -> str:
    payload = [RUNNER_VERSION, mode.value, dataset.dataset_id, manifest_checksum, config_hash]
    return (
        "crypto-exp-"
        + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:24]
    )


def _base_report(
    mode: CryptoExperimentMode,
    instrument: InstrumentMetadata,
    config: CryptoVWAPPullbackConfig,
    config_hash: str,
    manifest_path: Path,
    dataset: VerifiedDataset | None,
    manifest_checksum: str | None,
    *,
    status: str,
    reason: str | None = None,
    readiness_status: str | None = None,
    run_id: str | None = None,
    candidate_count: int = 0,
    candidate_rejection_counts: dict[str, int] | None = None,
    duplicate_candidate_count: int = 0,
) -> dict[str, Any]:
    deterministic_id = run_id or _blocked_run_id(mode, instrument, config_hash, status, reason)
    return {
        "report_version": RUNNER_VERSION,
        "status": status,
        "run_id": deterministic_id,
        "run_mode": mode.value,
        "synthetic_label": "SYNTHETIC"
        if mode == CryptoExperimentMode.SYNTHETIC_SELF_CHECK
        else None,
        "dataset_manifest_path": str(manifest_path),
        "dataset_manifest_id": dataset.dataset_id if dataset else None,
        "dataset_manifest_sha256": manifest_checksum,
        "dataset_output_hashes": dataset.artifact_sha256 if dataset else {},
        "dataset_raw_hashes": dataset.raw_sha256 if dataset else {},
        "instrument_id": instrument.instrument_id,
        "strategy_id": CRYPTO_VWAP_PULLBACK_ID,
        "strategy_version": config.version,
        "strategy_configuration_hash": config_hash,
        "vwap": {
            "reset_utc": config.utc_anchor.isoformat(),
            "price_input": config.price_input,
            "volume_input": config.volume_input,
            "candle_completion": config.candle_completion,
        },
        "candidate_count": candidate_count,
        "candidate_rejection_count_by_reason": candidate_rejection_counts or {},
        "duplicate_candidate_count": duplicate_candidate_count,
        "risk_approval_count": 0,
        "risk_rejection_count": 0,
        "submitted_orders": [],
        "accepted_orders": [],
        "rejected_orders": [],
        "fills": [],
        "fill_count": 0,
        "fees_by_currency": {},
        "account_balances_before": {},
        "account_balances_after": {},
        "ledger_reconciliation": {"passed": False, "errors": [reason] if reason else []},
        "execution_model": {"id": EXECUTION_MODEL_ID, "fill_model_id": FILL_MODEL_ID},
        "cost_model": {"fee_model_id": None, "cost_status": "COST_INCOMPLETE"},
        "readiness_status": readiness_status or status,
        "limitations": _limitations(mode, manifest_path.exists()),
        "blocked_reason": reason,
        "historical_pnl_metrics": None,
    }


def _report_from_simulation(
    mode: CryptoExperimentMode,
    instrument: InstrumentMetadata,
    config: CryptoVWAPPullbackConfig,
    config_hash: str,
    dataset: VerifiedDataset,
    manifest_checksum: str,
    account: CryptoSpotAccountConfig,
    fees: CryptoSpotFeeConfiguration,
    costs: CryptoSpotCostAssumptions,
    batch: CryptoCandidateBatch,
    candidates: tuple,
    duplicate_count: int,
    simulation: dict,
    run_id: str,
) -> dict[str, Any]:
    events = simulation["events"]
    event_ids = {event.get("order_id") for event in events}

    def order_records(event_name: str) -> list[dict]:
        return [
            {
                "order_id": event["order_id"],
                "candidate_id": event.get("candidate_id"),
                "timestamp": event["timestamp"],
                "role": event.get("role"),
            }
            for event in events
            if event["type"] == event_name and event.get("order_id") in event_ids
        ]

    fee_totals: Counter[str] = Counter()
    for fill in simulation["fills"]:
        fee_totals[fill["fee_currency"]] += Decimal(fill["fee"])
    risk_approvals = [event for event in events if event["type"] == "risk_approved"]
    risk_rejections = [event for event in events if event["type"] == "risk_rejected"]
    report = _base_report(
        mode,
        instrument,
        config,
        config_hash,
        Path(dataset.manifest_path),
        dataset,
        manifest_checksum,
        status="COMPLETE"
        if simulation.get("reconciliation", {}).get("passed")
        else "FAILED_RECONCILIATION",
        readiness_status=(
            "SYNTHETIC_SELF_CHECK_ONLY; not historical readiness"
            if mode == CryptoExperimentMode.SYNTHETIC_SELF_CHECK
            else "HISTORICAL_READY"
        ),
        run_id=run_id,
        candidate_count=len(candidates),
        candidate_rejection_counts=_candidate_rejections(batch),
        duplicate_candidate_count=duplicate_count,
    )
    report.update(
        {
            "candidate_decision_timestamps": [
                item.decision_timestamp.isoformat() for item in candidates
            ],
            "candidate_schedule": [
                {
                    "candidate_id": item.candidate_id,
                    "decision_timestamp": item.decision_timestamp.isoformat(),
                }
                for item in candidates
            ],
            "risk_approval_count": len(risk_approvals),
            "risk_rejection_count": len(risk_rejections),
            "risk_rejections": [
                {
                    "candidate_id": event.get("candidate_id"),
                    "reason": event.get("reason"),
                    "timestamp": event["timestamp"],
                }
                for event in risk_rejections
            ],
            "submitted_orders": order_records("order_submitted"),
            "accepted_orders": order_records("order_accepted"),
            "rejected_orders": order_records("order_rejected"),
            "fills": simulation["fills"],
            "fill_count": len(simulation["fills"]),
            "fees_by_currency": {key: str(value) for key, value in sorted(fee_totals.items())},
            "account_balances_before": {
                instrument.quote_currency: str(account.starting_quote_balance),
                instrument.base_currency: str(account.starting_base_balance),
            },
            "account_balances_after": simulation["nautilus_balances_total"],
            "account_type": simulation["account_type"],
            "account_currency": simulation["account_currency"],
            "account_authority": {
                "long_only": simulation["long_only"],
                "leverage_enabled": simulation["leverage_enabled"],
                "borrowing_enabled": simulation["borrowing_enabled"],
                "short_selling_enabled": simulation["short_selling_enabled"],
            },
            "ledger_reconciliation": simulation["reconciliation"],
            "execution_model": {
                "id": EXECUTION_MODEL_ID,
                "nautilus_version": simulation["nautilus_version"],
                "fill_model_id": FILL_MODEL_ID,
            },
            "cost_model": {
                "fee_model_id": fees.version,
                "cost_assumptions_id": costs.version,
            "execution_cost_model_id": "crypto-bps-cost-model-v1",
                "cost_status": simulation.get("cost_status", fees.cost_status),
                "source": costs.source,
            "spread_bps": str(costs.spread_bps),
            "slippage_bps": str(costs.slippage_bps),
            },
            "worker_events": events,
            "simulation_limitations": simulation.get("fill_limitations", []),
            "limitations": sorted(
                set(_limitations(mode, True) + simulation.get("fill_limitations", []))
            ),
            "blocked_reason": None,
        }
    )
    return report


def _blocked_run_id(
    mode: CryptoExperimentMode,
    instrument: InstrumentMetadata,
    config_hash: str,
    status: str,
    reason: str | None,
) -> str:
    encoded = json.dumps(
        [RUNNER_VERSION, mode.value, instrument.instrument_id, config_hash, status, reason],
        separators=(",", ":"),
    ).encode()
    return "crypto-exp-" + hashlib.sha256(encoded).hexdigest()[:24]


def _limitations(mode: CryptoExperimentMode, manifest_exists: bool) -> list[str]:
    limitations = [
        "Execution prices use OHLCV bar-event references with configured spread/slippage; no historical bid/ask or depth is available.",
        "Spread contributes to conservative risk sizing but is not applied to simulated fill prices.",
        "OHLCV bars do not contain historical order-book depth, queue position, or intraminute trade sequence.",
        "Nautilus fills and ledger values are simulation evidence, not live exchange behavior.",
    ]
    if mode == CryptoExperimentMode.SYNTHETIC_SELF_CHECK:
        limitations.insert(
            0, "SYNTHETIC outputs are software integration evidence, not trading performance."
        )
    if not manifest_exists:
        limitations.append("Dataset manifest or its referenced files are unavailable.")
    return limitations


def _persist_result(report: dict[str, Any], output_directory: Path) -> dict[str, Any]:
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = report["run_id"]
    json_path = output_directory / f"{run_id}.json"
    markdown_path = output_directory / f"{run_id}.md"
    report["report_artifacts"] = {
        "json": json_path.name,
        "markdown": markdown_path.name,
    }
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    report["report_json_path"] = str(json_path)
    report["report_markdown_path"] = str(markdown_path)
    return report


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Crypto experiment {report['run_id']}",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['run_mode']}`",
        f"- Synthetic label: `{report['synthetic_label'] or 'none'}`",
        f"- Dataset ID: `{report['dataset_manifest_id'] or 'unavailable'}`",
        f"- Dataset manifest SHA-256: `{report['dataset_manifest_sha256'] or 'unavailable'}`",
        f"- Instrument: `{report['instrument_id']}`",
        f"- Strategy: `{report['strategy_id']}@{report['strategy_version']}`",
        f"- Strategy configuration hash: `{report['strategy_configuration_hash']}`",
        f"- Candidates: {report['candidate_count']}",
        f"- Risk approvals / rejections: {report['risk_approval_count']} / {report['risk_rejection_count']}",
        f"- Fills: {report['fill_count']}",
        f"- Fees by currency: `{json.dumps(report['fees_by_currency'], sort_keys=True)}`",
        f"- Balances before: `{json.dumps(report['account_balances_before'], sort_keys=True)}`",
        f"- Balances after: `{json.dumps(report['account_balances_after'], sort_keys=True)}`",
        f"- Readiness: {report['readiness_status']}",
        f"- Report artifacts: `{report['report_artifacts']['json']}`, `{report['report_artifacts']['markdown']}`",
        "",
        "## Reconciliation",
        "",
        f"Passed: `{report['ledger_reconciliation'].get('passed', False)}`",
        f"Account P&L delta / tolerance: `{report['ledger_reconciliation'].get('account_pnl_delta_quote', 'n/a')}` / "
        f"`{report['ledger_reconciliation'].get('pnl_tolerance_quote', 'n/a')}` USDT",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    if report.get("fills"):
        lines.extend(["", "## Fills", ""])
        lines.extend(
            f"- {fill['timestamp']} {fill['side']} {fill['quantity']} @ {fill['price']} "
            f"fee {fill['fee']} {fill['fee_currency']} candidate `{fill['candidate_id']}`"
            for fill in report["fills"]
        )
    if report.get("blocked_reason"):
        lines.extend(["", "## Blocked reason", "", report["blocked_reason"]])
    lines.append("")
    return "\n".join(lines)
