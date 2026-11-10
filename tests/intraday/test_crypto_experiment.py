"""Synthetic end-to-end tests for the BTCUSDT experiment orchestrator."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tradingagents.intraday.crypto_exchange import parse_binance_spot_exchange_info
from tradingagents.intraday.crypto_execution import (
    CryptoSpotAccountConfig,
    CryptoSpotCostAssumptions,
    CryptoSpotExecutionSpec,
    CryptoSpotFeeConfiguration,
)
from tradingagents.intraday.crypto_experiment import run_crypto_experiment
from tradingagents.intraday.datasets import persist_dataset
from tradingagents.intraday.models import InstrumentMetadata, MarketBar

ROOT = Path(__file__).resolve().parents[2]
FILTERS = parse_binance_spot_exchange_info(
    (ROOT / "tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-raw.json").read_bytes(),
    retrieved_at=datetime.fromisoformat("2026-11-02T17:07:33.792021+00:00"),
)
NOW = FILTERS.retrieved_at_utc


def instrument(*, alias: bool = False) -> InstrumentMetadata:
    return InstrumentMetadata(
        "BINANCE_SPOT:BTCUSDT" if alias else "BTCUSDT.BINANCE_SPOT",
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
        "SYNTHETIC",
        "SYNTHETIC software integration fixture",
        D("0.00001"),
        D("5"),
    )


def execution_inputs(inst: InstrumentMetadata, *, quote_balance: str = "10000"):
    spec = CryptoSpotExecutionSpec(
        inst,
        "USDT",
        8,
        "USDT",
        "synthetic-fees-v1",
        "synthetic filter fixture",
        NOW,
        FILTERS,
    )
    account = CryptoSpotAccountConfig(
        "synthetic-crypto-account",
        "USDT",
        D(quote_balance),
        D(0),
        None,
        D("10"),
        D("100"),
        1,
        10,
        1440,
        "UTC",
        5,
    )
    fees = CryptoSpotFeeConfiguration(
        D("0.0008"),
        D("0.001"),
        "USDT",
        "SYNTHETIC test assumption",
        NOW,
        False,
        "synthetic-fees-v1",
    )
    costs = CryptoSpotCostAssumptions(
        fees.taker_model(),
        D("0.01"),
        D("2"),
        "SYNTHETIC test assumption",
        False,
        "synthetic-costs-v1",
    )
    return spec, account, fees, costs


def synthetic_manifest(tmp_path: Path) -> tuple[Path, InstrumentMetadata]:
    canonical = instrument()
    alias = instrument(alias=True)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars = []
    for day in range(2):
        previous = D("100")
        for group in range(288):  # Two complete synthetic 24-hour UTC sessions.
            close = D("100") + D("0.001") * group
            if group >= 201:
                close += D("0.02")
            if group >= 241:
                close += D("0.02")
            open_ = previous
            high = max(open_, close) + D("0.01")
            low = min(open_, close) - D("0.10")
            if group == 200:
                # A valid VWAP-zone wick, followed by a bullish reclaim at group 201.
                low = min(D("100.20"), open_, close)
            volume_per_minute = D("4") if group == 201 else D("2")
            for minute_in_group in range(5):
                timestamp = start + timedelta(days=day, minutes=group * 5 + minute_in_group)
                minute_high = high
                if group == 202 and minute_in_group == 1:
                    # Cross the generated stop-entry trigger on a later 1-minute event.
                    minute_high = max(minute_high, D("100.30"))
                bars.append(
                    MarketBar(
                        timestamp,
                        alias.instrument_id,
                        open_ if minute_in_group == 0 else close,
                        minute_high,
                        low,
                        close,
                        volume_per_minute,
                        f"synthetic-btcusdt-1m-{day}-{group:03d}-{minute_in_group}",
                        "SYNTHETIC deterministic fixture",
                        1,
                    )
                )
            previous = close
    manifest = persist_dataset(
        bars,
        alias,
        provider="synthetic",
        source_reference="SYNTHETIC deterministic integration fixture",
        requested_start=start.date(),
        requested_end=(start + timedelta(days=1)).date(),
        raw_payloads=[("synthetic-btcusdt.csv", b"SYNTHETIC; generated in test; no market data")],
        instrument_metadata_version="synthetic-btcusdt-v1",
        source_terms_reference="SYNTHETIC; not market data",
        data_root=tmp_path / "dataset",
    )
    manifest_path = (
        tmp_path
        / "dataset"
        / "market=24_7"
        / "instrument=BINANCE_SPOT_BTCUSDT"
        / "manifests"
        / f"dataset={manifest['dataset_id']}.json"
    )
    return manifest_path, canonical


def run(manifest: Path, inst: InstrumentMetadata, output: Path, *, quote_balance: str = "10000"):
    spec, account, fees, costs = execution_inputs(inst, quote_balance=quote_balance)
    return run_crypto_experiment(
        manifest,
        inst,
        mode="SYNTHETIC_SELF_CHECK",
        output_directory=output,
        execution_spec=spec,
        account=account,
        fee_configuration=fees,
        costs=costs,
    )


def test_synthetic_manifest_runs_real_strategy_through_nautilus_and_reconciles(tmp_path):
    manifest, inst = synthetic_manifest(tmp_path)
    report = run(manifest, inst, tmp_path / "reports")

    assert report["status"] == "COMPLETE", report
    assert report["run_mode"] == "SYNTHETIC_SELF_CHECK"
    assert report["synthetic_label"] == "SYNTHETIC"
    assert report["candidate_count"] >= 2
    assert report["risk_approval_count"] >= 1
    entry_candidate_ids = [
        order["candidate_id"] for order in report["submitted_orders"] if order["role"] == "entry"
    ]
    assert len(entry_candidate_ids) == report["risk_approval_count"]
    assert len(entry_candidate_ids) == len(set(entry_candidate_ids))
    assert report["fill_count"] >= 1
    assert report["ledger_reconciliation"]["passed"] is True
    assert report["dataset_manifest_id"]
    assert report["dataset_output_hashes"]["1m:2025-01"]
    assert report["historical_pnl_metrics"] is None
    assert report["cost_model"]["cost_status"] == "COST_INCOMPLETE"
    assert "Configured spread and slippage adjust fills" in " ".join(report["limitations"])
    assert Path(report["report_json_path"]).is_file()
    assert Path(report["report_markdown_path"]).is_file()
    saved = json.loads(Path(report["report_json_path"]).read_text(encoding="utf-8"))
    assert saved["report_artifacts"]["json"] == Path(report["report_json_path"]).name

    candidate = report["candidate_decision_timestamps"][0]
    candidate_events = [event for event in report["worker_events"] if event.get("candidate_id")]
    risk_event = next(event for event in candidate_events if event["type"] == "risk_approved")
    order_event = next(
        event for event in candidate_events if event["type"] == "order_submit_intent"
    )
    fill_event = next(event for event in candidate_events if event["type"] == "fill")
    assert risk_event["timestamp"] == candidate
    assert order_event["timestamp"] > candidate
    assert fill_event["timestamp"] > order_event["timestamp"]
    assert all(event["timestamp"] >= candidate for event in candidate_events)
    assert len(report["candidate_schedule"]) >= 2
    future_candidate = report["candidate_schedule"][1]
    future_events = [
        event
        for event in report["worker_events"]
        if event.get("candidate_id") == future_candidate["candidate_id"]
    ]
    assert future_events
    assert all(
        event["timestamp"] >= future_candidate["decision_timestamp"] for event in future_events
    )


def test_identical_inputs_produce_reproducible_reports(tmp_path):
    manifest, inst = synthetic_manifest(tmp_path)
    first = run(manifest, inst, tmp_path / "reports")
    second = run(manifest, inst, tmp_path / "reports")
    first.pop("report_json_path")
    first.pop("report_markdown_path")
    second.pop("report_json_path")
    second.pop("report_markdown_path")
    assert first == second


def test_risk_rejection_emits_no_order(tmp_path):
    manifest, inst = synthetic_manifest(tmp_path)
    report = run(manifest, inst, tmp_path / "reports", quote_balance="1")
    assert report["status"] == "COMPLETE"
    assert report["candidate_count"] >= 1
    assert report["risk_rejection_count"] >= 1
    assert report["submitted_orders"] == []
    assert report["fill_count"] == 0


def test_duplicate_generated_candidate_is_deduplicated_before_worker(tmp_path, monkeypatch):
    import tradingagents.intraday.crypto_experiment as experiment

    manifest, inst = synthetic_manifest(tmp_path)
    real_evaluate = experiment.evaluate_crypto_market_data

    def duplicate_batch(*args, **kwargs):
        batch = real_evaluate(*args, **kwargs)
        assert batch.candidates
        return replace(batch, candidates=batch.candidates + (batch.candidates[0],))

    monkeypatch.setattr(experiment, "evaluate_crypto_market_data", duplicate_batch)
    report = run(manifest, inst, tmp_path / "reports")
    assert report["duplicate_candidate_count"] == 1
    entry_candidate_ids = [
        order["candidate_id"] for order in report["submitted_orders"] if order["role"] == "entry"
    ]
    assert len(entry_candidate_ids) <= report["candidate_count"]
    assert len(entry_candidate_ids) == len(set(entry_candidate_ids))
    assert report["status"] == "COMPLETE"


@pytest.mark.parametrize("contents", [None, "not-json"])
def test_missing_or_invalid_manifest_blocks_without_worker(tmp_path, contents, monkeypatch):
    import tradingagents.intraday.crypto_experiment as experiment

    def forbidden(*args, **kwargs):
        raise AssertionError("worker must not run after invalid data preflight")

    monkeypatch.setattr(experiment, "run_crypto_nautilus_execution", forbidden)
    manifest = tmp_path / "missing.json"
    if contents is not None:
        manifest.write_text(contents, encoding="utf-8")
    report = run_crypto_experiment(
        manifest, instrument(), mode="SYNTHETIC_SELF_CHECK", output_directory=tmp_path / "reports"
    )
    assert report["status"] == "BLOCKED_DATA"
    assert report["fill_count"] == 0
    assert Path(report["report_json_path"]).is_file()


def test_missing_dataset_artifact_and_hash_mismatch_fail_closed(tmp_path, monkeypatch):
    import tradingagents.intraday.crypto_experiment as experiment

    manifest, inst = synthetic_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = data["artifacts"]["1m:2025-01"]["path"]
    Path(artifact).write_bytes(b"corrupted synthetic parquet")
    report = run_crypto_experiment(
        manifest, inst, mode="SYNTHETIC_SELF_CHECK", output_directory=tmp_path / "reports"
    )
    assert report["status"] == "BLOCKED_DATA"
    assert "checksum" in report["blocked_reason"].lower()

    def forbidden(*args, **kwargs):
        raise AssertionError("worker must not run after missing artifact preflight")

    monkeypatch.setattr(experiment, "run_crypto_nautilus_execution", forbidden)
    # Restore valid fixture in a separate manifest, then point it at an absent archive.
    manifest2, inst2 = synthetic_manifest(tmp_path / "second")
    data2 = json.loads(manifest2.read_text(encoding="utf-8"))
    Path(data2["raw_payloads"][0]["path"]).unlink()
    missing = run_crypto_experiment(
        manifest2, inst2, mode="SYNTHETIC_SELF_CHECK", output_directory=tmp_path / "reports2"
    )
    assert missing["status"] == "BLOCKED_DATA"


def test_instrument_mismatch_and_out_of_order_artifact_fail_closed(tmp_path):
    import pandas as pd

    from tradingagents.intraday.ingest import file_sha256

    manifest, inst = synthetic_manifest(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["instrument_id"] = "OTHER.BINANCE_SPOT"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    mismatch = run_crypto_experiment(
        manifest, inst, mode="SYNTHETIC_SELF_CHECK", output_directory=tmp_path / "mismatch"
    )
    assert mismatch["status"] == "BLOCKED_DATA"
    assert "instrument" in mismatch["blocked_reason"].lower()

    manifest2, inst2 = synthetic_manifest(tmp_path / "ordered")
    record2 = json.loads(manifest2.read_text(encoding="utf-8"))
    parquet = Path(record2["artifacts"]["1m:2025-01"]["path"])
    frame = pd.read_parquet(parquet)
    frame.iloc[[0, 1]] = frame.iloc[[1, 0]].to_numpy()
    frame.to_parquet(parquet, index=False)
    record2["artifacts"]["1m:2025-01"]["sha256"] = file_sha256(parquet)
    manifest2.write_text(json.dumps(record2), encoding="utf-8")
    unordered = run_crypto_experiment(
        manifest2, inst2, mode="SYNTHETIC_SELF_CHECK", output_directory=tmp_path / "unordered"
    )
    assert unordered["status"] == "BLOCKED_DATA"
    assert "out-of-order" in unordered["blocked_reason"].lower()


def test_historical_mode_stays_blocked_without_validated_readiness(tmp_path, monkeypatch):
    import tradingagents.intraday.crypto_experiment as experiment

    manifest, inst = synthetic_manifest(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("historical execution must remain gated")

    monkeypatch.setattr(experiment, "run_crypto_nautilus_execution", forbidden)
    spec, account, fees, costs = execution_inputs(inst)
    report = run_crypto_experiment(
        manifest,
        inst,
        mode="HISTORICAL",
        output_directory=tmp_path / "reports",
        execution_spec=spec,
        account=account,
        fee_configuration=fees,
        costs=costs,
    )
    assert report["status"] == "BLOCKED_READINESS"
    assert report["historical_pnl_metrics"] is None


def test_failed_ledger_reconciliation_fails_the_experiment(tmp_path, monkeypatch):
    import tradingagents.intraday.crypto_experiment as experiment

    manifest, inst = synthetic_manifest(tmp_path)

    def failed_reconciliation(*args, **kwargs):
        return {
            "events": [],
            "fills": [],
            "cost_status": "COST_INCOMPLETE",
            "fill_limitations": [],
            "nautilus_version": "1.231.0",
            "account_type": "CASH",
            "account_currency": "USDT",
            "long_only": True,
            "leverage_enabled": False,
            "borrowing_enabled": False,
            "short_selling_enabled": False,
            "nautilus_balances_total": {"USDT": "10000", "BTC": "0"},
            "reconciliation": {"passed": False, "errors": ["synthetic mismatch"]},
        }

    monkeypatch.setattr(experiment, "run_crypto_nautilus_execution", failed_reconciliation)
    spec, account, fees, costs = execution_inputs(inst)
    report = run_crypto_experiment(
        manifest,
        inst,
        mode="SYNTHETIC_SELF_CHECK",
        output_directory=tmp_path / "reports",
        execution_spec=spec,
        account=account,
        fee_configuration=fees,
        costs=costs,
    )
    assert report["status"] == "FAILED_RECONCILIATION"
    assert report["ledger_reconciliation"]["passed"] is False
