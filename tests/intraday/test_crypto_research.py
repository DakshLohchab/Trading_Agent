"""Focused synthetic validation of BTCUSDT research assumptions and readiness."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from tradingagents.intraday.crypto_costs import CryptoExecutionCostModel
from tradingagents.intraday.crypto_research import (
    CostClassification,
    CryptoResearchProfile,
    FeeClassification,
    canonical_hash,
    cost_sensitivity_scenarios,
)
from tradingagents.intraday.crypto_research_readiness import (
    build_crypto_research_readiness,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "tradingagents/intraday/config/btcusdt_research.example.yaml"
FILTERS = ROOT / "tradingagents/intraday/data/binance/BTCUSDT-exchangeInfo-normalized.json"
MISSING_MANIFEST = Path("/private/tmp/tradingagents-phase4b-data/absent-manifest.json")


def configured_profile() -> CryptoResearchProfile:
    empty = CryptoResearchProfile.load_yaml(TEMPLATE)
    return replace(
        empty,
        starting_capital=D("5000"),
        max_risk_per_trade=D("25"),
        max_daily_loss=D("100"),
        max_concurrent_positions=1,
        max_trades_per_utc_day=4,
        maker_fee_bps=D("8"),
        taker_fee_bps=D("10"),
        fee_source="SYNTHETIC test assumption",
        fee_configured_at=datetime(2026, 11, 10, tzinfo=UTC),
        fee_classification=FeeClassification.RESEARCH_ASSUMPTION,
        spread_bps=D("2"),
        spread_source="SYNTHETIC test assumption",
        spread_classification=CostClassification.RESEARCH_ASSUMPTION,
        slippage_bps=D("1"),
        slippage_source="SYNTHETIC test assumption",
        slippage_classification=CostClassification.RESEARCH_ASSUMPTION,
    )


def test_example_profile_keeps_user_dependent_values_unset():
    profile = CryptoResearchProfile.load_yaml(TEMPLATE)
    assert profile.starting_capital is None
    assert profile.max_risk_per_trade is None
    assert profile.max_daily_loss is None
    assert profile.maker_fee_bps is None
    assert profile.taker_fee_bps is None
    assert profile.spread_bps is None
    assert profile.slippage_bps is None
    assert profile.account_currency == "USDT"
    assert profile.account_id != "equity-inr"
    assert set(profile.blockers()) >= {
        "ACCOUNT_CAPITAL_UNCONFIGURED",
        "RISK_PER_TRADE_UNCONFIGURED",
        "DAILY_LOSS_LIMIT_UNCONFIGURED",
        "MAKER_FEE_UNCONFIGURED",
        "TAKER_FEE_UNCONFIGURED",
        "SPREAD_UNCONFIGURED",
        "SLIPPAGE_UNCONFIGURED",
    }
    with pytest.raises(ValueError, match="incomplete"):
        profile.runtime_configs()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.1", "not-a-number"])
def test_decimal_fields_reject_invalid_or_negative_values(value):
    empty = CryptoResearchProfile.load_yaml(TEMPLATE)
    with pytest.raises(ValueError):
        replace(empty, slippage_bps=value)


def test_invalid_capital_and_unsupported_fee_currency_fail_closed():
    empty = CryptoResearchProfile.load_yaml(TEMPLATE)
    with pytest.raises(ValueError, match="capital"):
        replace(empty, starting_capital=D(0))
    with pytest.raises(ValueError, match="USDT"):
        replace(empty, fee_currency="BNB")


def test_complete_profile_builds_separate_usdt_research_runtime_configuration():
    account, fees, costs = configured_profile().runtime_configs()
    assert account.account_id == "btc-usdt-research"
    assert account.starting_quote_balance == D("5000")
    assert account.max_risk_per_trade == D("25")
    assert account.account_currency == "USDT"
    assert account.long_only and not account.leverage_enabled
    assert not account.borrowing_enabled and not account.short_selling_enabled
    assert fees.maker_rate == D("0.0008")
    assert fees.taker_rate == D("0.001")
    assert fees.actual_account_rates is False
    assert costs.spread_bps == D("2") and costs.slippage_bps == D("1")
    assert costs.cost_status == "COST_INCOMPLETE"


def test_spread_slippage_adjustment_and_adverse_tick_rounding():
    costs = CryptoExecutionCostModel(D("2"), D("1"))
    assert costs.adjustment(D("100")) == D("0.02")
    assert costs.adjusted_price(D("100"), "BUY", D("0.01")) == D("100.02")
    assert costs.adjusted_price(D("100"), "SELL", D("0.01")) == D("99.98")
    assert costs.adjusted_price(D("100.004"), "BUY", D("0.01")) == D("100.03")
    assert costs.adjusted_price(D("100.004"), "SELL", D("0.01")) == D("99.98")


def test_slippage_is_price_dependent_and_shared_model_is_deterministic():
    costs = CryptoExecutionCostModel(D("0"), D("10"))
    assert costs.adjustment(D("200")) == 2 * costs.adjustment(D("100"))
    assert canonical_hash({"price": D("100.0")}) == canonical_hash({"price": D("100.0")})
    assert canonical_hash({"price": D("100.0")}) != canonical_hash({"price": D("100.1")})


def test_exactly_three_cost_scenarios_preserve_spread_and_hash_changes():
    profile = configured_profile()
    scenarios = cost_sensitivity_scenarios(profile)
    assert [item.name for item in scenarios] == ["BASE", "COST_1_5X", "COST_2X"]
    assert [item.spread_bps for item in scenarios] == [D("2"), D("2"), D("2")]
    assert [item.taker_fee_bps for item in scenarios] == [D("10"), D("15.0"), D("20.0")]
    assert [item.slippage_bps for item in scenarios] == [D("1"), D("1.5"), D("2.0")]
    assert len({item.configuration_hash for item in scenarios}) == 3
    with pytest.raises((AttributeError, TypeError)):
        scenarios[0].name = "changed"
    assert cost_sensitivity_scenarios(profile) == scenarios


def test_readiness_executes_only_synthetic_integration_and_reports_data_blocker():
    profile = CryptoResearchProfile.load_yaml(TEMPLATE)
    report = build_crypto_research_readiness(
        manifest_path=MISSING_MANIFEST,
        config_path=TEMPLATE,
        filter_record_path=FILTERS,
    )
    assert report["status"] == "BLOCKED_DATA"
    assert any(item["code"] == "DATASET_MANIFEST_MISSING" for item in report["blockers"])
    assert report["synthetic_integration_self_check"]["executed"] is True
    assert report["synthetic_integration_self_check"]["status"] == "COMPLETE"
    assert report["synthetic_integration_self_check"]["synthetic_only"] is True
    assert report["synthetic_integration_self_check"]["ledger_reconciliation_passed"] is True
    assert report["synthetic_integration_self_check"]["risk_precedes_submission"] is True
    assert report["synthetic_integration_self_check"]["cost_adjusted_fills"] is True
    assert report["synthetic_integration_self_check"]["historical_performance_metrics"] is None
    assert report["historical_backtest_executed"] is False
    assert (
        report["configuration_snapshot"]["filter_snapshot"]["classification"]
        == "CURRENT_ONLY_NOT_JANUARY_2025_RULES"
    )
    assert report["configuration_snapshot"]["account_and_cost_profile"] == profile.snapshot()


def test_filter_normalized_hash_mismatch_is_reported(tmp_path):
    wrapper = json.loads(FILTERS.read_text(encoding="utf-8"))
    wrapper["filters"]["raw_response_sha256"] = "0" * 64
    tampered = tmp_path / "tampered-filter.json"
    tampered.write_text(json.dumps(wrapper), encoding="utf-8")
    report = build_crypto_research_readiness(
        manifest_path=MISSING_MANIFEST,
        config_path=TEMPLATE,
        filter_record_path=tampered,
    )
    assert report["status"] == "BLOCKED_DATA"
    assert any(item["code"] == "FILTER_SNAPSHOT_HASH_MISMATCH" for item in report["blockers"])
    assert report["synthetic_integration_self_check"]["executed"] is False
