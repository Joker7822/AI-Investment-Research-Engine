from datetime import datetime, timezone

from app.domain.schemas import AnalysisRequest, AssetRef
from app.providers.derived import DerivedFinancialsProvider, derive_edinet_metrics


def metric(
    value: str,
    *,
    doc_id: str = "S100TEST",
    unit_id: str = "JPY",
    scope: str = "consolidated",
    standard: str = "jp_gaap",
    period_key: str = "CurrentYear",
    element_id: str = "test:Element",
):
    return {
        "numeric_value": value,
        "raw_value": value,
        "unit_id": unit_id,
        "unit_label": "円",
        "scope": scope,
        "accounting_standard": standard,
        "mapping_version": "2026-10-14-v1",
        "element_id": element_id,
        "context_id": f"{period_key}Duration",
        "relative_year": "当期" if period_key == "CurrentYear" else "前期",
        "doc_id": doc_id,
        "source_file": "XBRL_TO_CSV/test.csv",
        "row_number": 2,
    }


def complete_metrics():
    return {
        "revenue": {
            "CurrentYear": metric("1200", element_id="revenue"),
            "Prior1Year": metric("1000", period_key="Prior1Year", element_id="revenue"),
        },
        "operating_income": {"CurrentYear": metric("120", element_id="op")},
        "net_income": {"CurrentYear": metric("90", element_id="net")},
        "cf_operating": {"CurrentYear": metric("150", element_id="cfo")},
        "total_assets": {
            "CurrentYear": metric("2200", element_id="assets"),
            "Prior1Year": metric("2000", period_key="Prior1Year", element_id="assets"),
        },
        "net_assets": {
            "CurrentYear": metric("1100", element_id="equity"),
            "Prior1Year": metric("900", period_key="Prior1Year", element_id="equity"),
        },
    }


def test_derives_core_ratios_with_decimal_math_and_provenance():
    derived, skipped = derive_edinet_metrics(complete_metrics())

    assert derived["revenue_growth_yoy"]["value"] == "0.2"
    assert derived["operating_margin"]["value"] == "0.1"
    assert derived["net_margin"]["value"] == "0.075"
    assert derived["operating_cash_flow_margin"]["value"] == "0.125"
    assert derived["asset_growth_yoy"]["value"] == "0.1"
    assert derived["equity_growth_yoy"]["value"] == "0.2222222222222222222222222222222222"
    assert derived["equity_ratio_calculated"]["value"] == "0.5"
    assert derived["cash_conversion"]["value"] == "1.666666666666666666666666666666667"
    assert derived["roa_calculated"]["value"] == "0.04285714285714285714285714285714286"
    assert derived["roe_calculated"]["value"] == "0.09"
    assert skipped == []

    revenue_inputs = derived["revenue_growth_yoy"]["inputs"]
    assert revenue_inputs[0]["doc_id"] == "S100TEST"
    assert revenue_inputs[0]["element_id"] == "revenue"
    assert revenue_inputs[1]["period_key"] == "Prior1Year"


def test_refuses_to_mix_different_filings():
    metrics = complete_metrics()
    metrics["revenue"]["Prior1Year"] = metric(
        "1000",
        doc_id="S100OLD",
        period_key="Prior1Year",
        element_id="revenue",
    )

    derived, skipped = derive_edinet_metrics(metrics)

    assert "revenue_growth_yoy" not in derived
    assert {item["metric"]: item["reason"] for item in skipped}["revenue_growth_yoy"] == "source_filing_mismatch"


def test_refuses_scope_and_unit_mismatches():
    metrics = complete_metrics()
    metrics["operating_income"]["CurrentYear"] = metric("120", scope="individual", element_id="op")
    metrics["cf_operating"]["CurrentYear"] = metric("150", unit_id="USD", element_id="cfo")

    derived, skipped = derive_edinet_metrics(metrics)
    reasons = {item["metric"]: item["reason"] for item in skipped}

    assert "operating_margin" not in derived
    assert reasons["operating_margin"] == "scope_mismatch"
    assert "operating_cash_flow_margin" not in derived
    assert reasons["operating_cash_flow_margin"] == "unit_mismatch"


def test_zero_denominator_is_not_emitted():
    metrics = complete_metrics()
    metrics["revenue"]["Prior1Year"] = metric("0", period_key="Prior1Year", element_id="revenue")
    metrics["net_income"]["CurrentYear"] = metric("0", element_id="net")

    derived, skipped = derive_edinet_metrics(metrics)
    reasons = {item["metric"]: item["reason"] for item in skipped}

    assert "revenue_growth_yoy" not in derived
    assert reasons["revenue_growth_yoy"] == "zero_denominator"
    assert "cash_conversion" not in derived
    assert reasons["cash_conversion"] == "zero_denominator"


def test_negative_equity_keeps_math_but_marks_economic_instability():
    metrics = complete_metrics()
    metrics["net_assets"]["CurrentYear"] = metric("-100", element_id="equity")
    metrics["net_assets"]["Prior1Year"] = metric("-200", period_key="Prior1Year", element_id="equity")

    derived, _ = derive_edinet_metrics(metrics)

    assert derived["roe_calculated"]["value"] == "-0.6"
    assert "non_positive_balance_makes_ratio_economically_unstable" in derived["roe_calculated"]["notes"]


async def test_provider_appends_derived_metrics_without_mutating_source_metrics():
    source_metrics = complete_metrics()
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="7203.T", exchange="TSE", region="JP"),
        user_question="Analyze Toyota",
        fundamental_data={
            "edinet": {
                "sec_code": "72030",
                "metrics": source_metrics,
            }
        },
    )

    result = await DerivedFinancialsProvider().enrich(request)

    assert "derived" not in request.fundamental_data["edinet"]
    assert result.fundamental_data["edinet"]["derived"]["metrics"]["operating_margin"]["value"] == "0.1"
    assert result.fundamental_data["edinet"]["metrics"] == source_metrics
