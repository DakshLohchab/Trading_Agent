"""Explicit readiness gate for a future BTC/USDT historical backtest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.experiment_validation import (
    DatasetVerificationError,
    VerifiedDataset,
    verify_dataset_manifest,
)
from tradingagents.intraday.models import InstrumentMetadata


def crypto_backtest_readiness(
    *,
    manifest_path: str | Path,
    instrument: InstrumentMetadata,
    filter_record_path: str | Path,
    fee_configuration: CryptoSpotFeeConfiguration | None,
    costs: CryptoSpotCostAssumptions | None,
    account: CryptoSpotAccountConfig | None,
    nautilus_integration_validated: bool,
    ledger_reconciliation_passed: bool,
) -> dict:
    """Return a fail-closed, machine-readable backtest gate; never runs a backtest."""
    dataset: VerifiedDataset | None = None
    dataset_error = None
    try:
        dataset = verify_dataset_manifest(
            manifest_path,
            instrument,
            expected_dataset_id="819f13eb67b47c7a01634c10",
            expected_counts={"1m": 44_640, "5m": 8_928, "15m": 2_976},
            expected_actual_range_utc=(
                "2025-01-01T00:00:00+00:00",
                "2025-01-31T23:59:00+00:00",
            ),
        )
    except (DatasetVerificationError, OSError, ValueError) as exc:
        dataset_error = str(exc)
    filters = None
    filter_error = None
    try:
        record_path = Path(filter_record_path)
        wrapper = json.loads(record_path.read_text(encoding="utf-8"))
        raw_path = Path(wrapper["raw_response_path"])
        raw_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        filters = wrapper["filters"]
        if wrapper.get("snapshot_kind") != "current_exchange_rules_not_historical_rules":
            raise ValueError("filter snapshot is not marked as current-only")
        if filters["raw_response_sha256"] != raw_digest:
            raise ValueError("raw Binance exchangeInfo checksum mismatch")
        if filters["symbol"] != "BTCUSDT" or not filters["spot_enabled"]:
            raise ValueError("BTCUSDT Spot filters are unavailable")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        filter_error = str(exc)
    fee_explicit = fee_configuration is not None
    spread_explicit = costs is not None and costs.spread_bps is not None
    slippage_explicit = costs is not None and costs.slippage_bps is not None
    fee_currency_supported = bool(
        fee_configuration
        and fee_configuration.fee_currency == instrument.quote_currency
    )
    fee_status = (
        fee_configuration.cost_status
        if fee_configuration is not None
        else "COST_INCOMPLETE"
    )
    cost_inputs_explicit = fee_explicit and spread_explicit and slippage_explicit and fee_currency_supported
    risk_valid = account is not None and account.account_currency == instrument.quote_currency
    checks = {
        "dataset_verified": dataset is not None,
        "current_instrument_filters_available": filters is not None,
        "fee_configuration_explicit": fee_explicit,
        "spread_configuration_explicit": spread_explicit,
        "slippage_configuration_explicit": slippage_explicit,
        "crypto_account_capital_configured": account is not None,
        "risk_configuration_valid": risk_valid,
        "nautilus_integration_validated": nautilus_integration_validated,
        "ledger_reconciliation_passed": ledger_reconciliation_passed,
    }
    ready = all(checks.values())
    return {
        "report_version": "crypto-backtest-readiness-v1",
        "dataset_status": "verified" if dataset is not None else "rejected",
        "dataset": asdict(dataset) if dataset is not None else None,
        "dataset_error": dataset_error,
        "current_instrument_filters_status": "available" if filters is not None else "unavailable",
        "filter_snapshot": filters,
        "filter_error": filter_error,
        "filter_snapshot_is_historical": False,
        "historical_filter_compatibility": "unverified_current_snapshot_only",
        "fee_configuration_status": fee_status,
        "fee_configuration_explicit": fee_explicit,
        "fee_currency_supported_by_nautilus_projection": fee_currency_supported,
        "spread_configuration_status": "complete" if spread_explicit else "incomplete",
        "slippage_configuration_status": "complete" if slippage_explicit else "incomplete",
        "cost_inputs_explicit": cost_inputs_explicit,
        "account_capital_status": "configured" if account is not None else "missing",
        "risk_configuration_status": "valid" if risk_valid else "invalid_or_missing",
        "nautilus_integration_status": "validated" if nautilus_integration_validated else "not_validated",
        "ledger_reconciliation_status": "passed" if ledger_reconciliation_passed else "not_passed",
        "checks": checks,
        "ready_for_historical_backtest": ready,
        "realistic_net_performance_label_allowed": ready and fee_status == "COST_COMPLETE",
        "historical_backtest_executed": False,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic BTCUSDT research readiness command."""
    from tradingagents.intraday.crypto_research_readiness import main as research_main

    return research_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
