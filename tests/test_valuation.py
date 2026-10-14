from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.schemas import (
    AnalysisRequest,
    AssetRef,
    DCFScenarioAssumption,
    MultipleScenarioAssumption,
    ValuationAssumptions,
)
from app.providers.valuation import (
    ValuationCalculationProvider,
    calculate_dcf,
    calculate_multiple,
)


def test_dcf_gordon_growth_fcff_is_deterministic():
    scenario = DCFScenarioAssumption(
        name="base",
        currency="USD",
        cash_flow_basis="fcff",
        base_fcf=Decimal("100"),
        growth_rates=[Decimal("0.10"), Decimal("0.10")],
        wacc=Decimal("0.10"),
        terminal_growth=Decimal("0.02"),
        net_debt=Decimal("20"),
        diluted_shares=Decimal("10"),
    )

    result = calculate_dcf(scenario)

    assert result["forecast"] == [
        {
            "year": 1,
            "growth_rate": "0.10",
            "fcf": "110.00",
            "discount_factor": "1.10",
            "present_value": "100",
        },
        {
            "year": 2,
            "growth_rate": "0.10",
            "fcf": "121.0000",
            "discount_factor": "1.2100",
            "present_value": "100",
        },
    ]
    assert result["pv_forecast_fcf"] == "200"
    assert result["terminal_fcf"] == "123.420000"
    assert result["terminal_value"] == "1542.7500"
    assert result["pv_terminal_value"] == "1275"
    assert result["value_basis"] == "enterprise"
    assert result["enterprise_value"] == "1475"
    assert result["equity_value"] == "1455"
    assert result["implied_value_per_share"] == "145.5"
    assert result["assumptions"]["cash_flow_basis"] == "fcff"
    assert result["assumptions"]["discount_rate_name"] == "wacc"
    assert result["assumptions"]["applied_discount_rate"] == "0.10"
    assert result["classification"] == "assumption_based_calculation_not_evidence"


def test_dcf_fcfe_values_equity_directly_and_uses_cost_of_equity():
    scenario = DCFScenarioAssumption(
        name="base",
        currency="USD",
        cash_flow_basis="fcfe",
        base_fcf=Decimal("100"),
        growth_rates=[Decimal("0.10"), Decimal("0.10")],
        cost_of_equity=Decimal("0.10"),
        terminal_growth=Decimal("0.02"),
        net_debt=Decimal("20"),
        diluted_shares=Decimal("10"),
    )

    result = calculate_dcf(scenario)

    assert result["value_basis"] == "equity"
    assert result["equity_value"] == "1475"
    assert result["enterprise_value"] == "1495"
    assert result["implied_value_per_share"] == "147.5"
    assert result["assumptions"]["discount_rate_name"] == "cost_of_equity"
    assert result["assumptions"]["applied_discount_rate"] == "0.10"
    assert "fcfe_values_equity_directly_net_debt_not_subtracted" in result["warnings"]


def test_dcf_rejects_wacc_not_above_terminal_growth():
    with pytest.raises(ValidationError, match="wacc > terminal_growth"):
        DCFScenarioAssumption(
            currency="USD",
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("0.05")],
            wacc=Decimal("0.02"),
            terminal_growth=Decimal("0.02"),
            diluted_shares=Decimal("10"),
        )


def test_dcf_rejects_mixed_or_missing_discount_rate_basis():
    with pytest.raises(ValidationError, match="FCFE DCF requires cost_of_equity"):
        DCFScenarioAssumption(
            currency="USD",
            cash_flow_basis="fcfe",
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("0.05")],
            terminal_growth=Decimal("0.02"),
            diluted_shares=Decimal("10"),
        )

    with pytest.raises(ValidationError, match="FCFE DCF must not supply wacc"):
        DCFScenarioAssumption(
            currency="USD",
            cash_flow_basis="fcfe",
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("0.05")],
            wacc=Decimal("0.10"),
            cost_of_equity=Decimal("0.11"),
            terminal_growth=Decimal("0.02"),
            diluted_shares=Decimal("10"),
        )

    with pytest.raises(ValidationError, match="FCFF DCF must not supply cost_of_equity"):
        DCFScenarioAssumption(
            currency="USD",
            cash_flow_basis="fcff",
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("0.05")],
            wacc=Decimal("0.10"),
            cost_of_equity=Decimal("0.11"),
            terminal_growth=Decimal("0.02"),
            diluted_shares=Decimal("10"),
        )


def test_dcf_rejects_growth_at_or_below_minus_100_percent():
    with pytest.raises(ValidationError, match="growth rate must be greater than -1"):
        DCFScenarioAssumption(
            currency="USD",
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("-1")],
            wacc=Decimal("0.10"),
            terminal_growth=Decimal("0.02"),
            diluted_shares=Decimal("10"),
        )


def test_multiple_calculator_handles_enterprise_and_equity_basis():
    enterprise = calculate_multiple(
        MultipleScenarioAssumption(
            name="ev_ebitda",
            currency="USD",
            metric_name="ebitda",
            metric_value=Decimal("100"),
            multiple=Decimal("8"),
            basis="enterprise",
            net_debt=Decimal("200"),
            diluted_shares=Decimal("10"),
        )
    )
    assert enterprise["enterprise_value"] == "800"
    assert enterprise["equity_value"] == "600"
    assert enterprise["implied_value_per_share"] == "60"

    equity = calculate_multiple(
        MultipleScenarioAssumption(
            name="pe",
            currency="USD",
            metric_name="earnings",
            metric_value=Decimal("50"),
            multiple=Decimal("15"),
            basis="equity",
            net_debt=Decimal("200"),
            diluted_shares=Decimal("10"),
        )
    )
    assert equity["equity_value"] == "750"
    assert equity["enterprise_value"] == "950"
    assert equity["implied_value_per_share"] == "75"


def test_multiple_rejects_non_positive_metric_or_multiple():
    with pytest.raises(ValidationError):
        MultipleScenarioAssumption(
            name="invalid",
            currency="USD",
            metric_name="earnings",
            metric_value=Decimal("0"),
            multiple=Decimal("10"),
            basis="equity",
            diluted_shares=Decimal("10"),
        )
    with pytest.raises(ValidationError):
        MultipleScenarioAssumption(
            name="invalid",
            currency="USD",
            metric_name="earnings",
            metric_value=Decimal("10"),
            multiple=Decimal("0"),
            basis="equity",
            diluted_shares=Decimal("10"),
        )


def test_scenario_names_must_be_unique_within_method():
    scenario = DCFScenarioAssumption(
        name="base",
        currency="USD",
        base_fcf=Decimal("100"),
        growth_rates=[Decimal("0.05")],
        wacc=Decimal("0.10"),
        terminal_growth=Decimal("0.02"),
        diluted_shares=Decimal("10"),
    )
    with pytest.raises(ValidationError, match="scenario names must be unique"):
        ValuationAssumptions(dcf=[scenario, scenario])


async def test_provider_adds_market_comparison_only_for_matching_currency():
    assumptions = ValuationAssumptions(
        dcf=[
            DCFScenarioAssumption(
                name="base",
                currency="USD",
                base_fcf=Decimal("100"),
                growth_rates=[Decimal("0.10"), Decimal("0.10")],
                wacc=Decimal("0.10"),
                terminal_growth=Decimal("0.02"),
                net_debt=Decimal("20"),
                diluted_shares=Decimal("10"),
            )
        ]
    )
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="AAPL", exchange="NASDAQ", currency="USD", region="US"),
        user_question="Analyze Apple",
        market_data={
            "massive": {
                "price_adjustment": "unadjusted",
                "bars": [
                    {"date": "2026-10-13", "close": 100},
                ],
            }
        },
        valuation_assumptions=assumptions,
    )

    result = await ValuationCalculationProvider().enrich(request)
    dcf = result.fundamental_data["valuation_calculations"]["dcf"][0]

    assert dcf["market_comparison"]["market_date"] == "2026-10-13"
    assert dcf["market_comparison"]["market_price"] == "100"
    assert dcf["market_comparison"]["implied_premium_discount"] == "0.455"
    assert result.evidence == []

    mismatch = request.model_copy(
        update={
            "valuation_assumptions": ValuationAssumptions(
                dcf=[assumptions.dcf[0].model_copy(update={"currency": "JPY"})]
            )
        }
    )
    mismatch_result = await ValuationCalculationProvider().enrich(mismatch)
    mismatch_dcf = mismatch_result.fundamental_data["valuation_calculations"]["dcf"][0]
    assert "market_comparison" not in mismatch_dcf


async def test_provider_is_noop_without_explicit_assumptions():
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="AAPL", currency="USD"),
        user_question="Analyze Apple",
    )
    result = await ValuationCalculationProvider().enrich(request)
    assert result is request
