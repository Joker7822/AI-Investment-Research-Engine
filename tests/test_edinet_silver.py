from decimal import Decimal

from app.metrics.edinet_silver import (
    detect_accounting_standard,
    extract_silver_metrics,
    parse_numeric,
)
from app.providers.edinet import select_runtime_metrics
from app.providers.edinet_csv import EdinetCsvFact


def fact(
    element_id: str,
    value: str,
    *,
    context_id: str = "CurrentYearDuration",
    item_name: str = "test",
    relative_year: str = "当期",
    consolidation: str = "連結",
    period_type: str = "期間",
    unit_id: str = "JPY",
    unit_label: str = "円",
    row_number: int = 2,
) -> EdinetCsvFact:
    return EdinetCsvFact(
        source_file="XBRL_TO_CSV/test.csv",
        row_number=row_number,
        element_id=element_id,
        item_name=item_name,
        context_id=context_id,
        relative_year=relative_year,
        consolidation=consolidation,
        period_type=period_type,
        unit_id=unit_id,
        unit_label=unit_label,
        value=value,
    )


def test_parse_numeric_preserves_auditable_numeric_value():
    assert parse_numeric("1,234") == Decimal("1234")
    assert parse_numeric("△1,234") == Decimal("-1234")
    assert parse_numeric("(123.4)") == Decimal("-123.4")
    assert parse_numeric("12.5%") == Decimal("12.5")
    assert parse_numeric("—") is None
    assert parse_numeric("narrative") is None


def test_jp_gaap_exact_match_and_annual_context_only():
    facts = [
        fact("jpcrp_cor:NetSalesSummaryOfBusinessResults", "1000000", row_number=2),
        fact(
            "jpcrp_cor:NetSalesSummaryOfBusinessResults",
            "900000",
            context_id="Prior1YearDuration",
            relative_year="前期",
            row_number=3,
        ),
        fact(
            "jpcrp_cor:NetSalesSummaryOfBusinessResults",
            "500000",
            context_id="CurrentYearDuration_OperatingSegmentsMember",
            row_number=4,
        ),
        fact(
            "custom:NetSalesSummaryOfBusinessResults",
            "9999999",
            item_name="売上高",
            row_number=5,
        ),
    ]

    version, standard, metrics = extract_silver_metrics(facts)

    assert version == "2026-10-14-v1"
    assert standard == "jp_gaap"
    revenue = [m for m in metrics if m.metric_name == "revenue"]
    assert [(m.period_key, m.numeric_value) for m in revenue] == [
        ("CurrentYear", Decimal("1000000")),
        ("Prior1Year", Decimal("900000")),
    ]
    assert all(m.element_id == "jpcrp_cor:NetSalesSummaryOfBusinessResults" for m in revenue)


def test_duration_metric_rejects_instant_context():
    _, _, metrics = extract_silver_metrics([
        fact(
            "jpcrp_cor:NetSalesSummaryOfBusinessResults",
            "100",
            context_id="CurrentYearInstant",
            period_type="時点",
        )
    ])
    assert [m for m in metrics if m.metric_name == "revenue"] == []


def test_instant_metric_and_nonconsolidated_scope_are_preserved():
    _, standard, metrics = extract_silver_metrics([
        fact(
            "jpcrp_cor:TotalAssetsSummaryOfBusinessResults",
            "2000000",
            context_id="CurrentYearInstant_NonConsolidatedMember",
            consolidation="個別",
            period_type="時点",
            row_number=8,
        )
    ])
    assert standard == "jp_gaap"
    total_assets = next(m for m in metrics if m.metric_name == "total_assets")
    assert total_assets.scope == "individual"
    assert total_assets.period_key == "CurrentYear"
    assert total_assets.numeric_value == Decimal("2000000")


def test_accounting_standard_detection_ifrs_and_us_gaap():
    assert detect_accounting_standard([
        fact("jpcrp_cor:RevenueIFRSSummaryOfBusinessResults", "100")
    ]) == "ifrs"
    assert detect_accounting_standard([
        fact("jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults", "100")
    ]) == "us_gaap"


def test_ifrs_and_us_gaap_use_their_own_exact_mappings():
    _, standard_ifrs, ifrs_metrics = extract_silver_metrics([
        fact("jpcrp_cor:RevenueIFRSSummaryOfBusinessResults", "500")
    ])
    assert standard_ifrs == "ifrs"
    assert next(m for m in ifrs_metrics if m.metric_name == "revenue").numeric_value == Decimal("500")

    _, standard_us, us_metrics = extract_silver_metrics([
        fact("jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults", "700")
    ])
    assert standard_us == "us_gaap"
    assert next(m for m in us_metrics if m.metric_name == "revenue").numeric_value == Decimal("700")


def test_mapping_priority_is_retained_for_multiple_allowed_elements():
    _, standard, metrics = extract_silver_metrics([
        fact(
            "jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
            "80",
            row_number=2,
        ),
        fact("jpcrp_cor:NetIncomeLossSummaryOfBusinessResults", "75", row_number=3),
    ])
    assert standard == "jp_gaap"
    net_income = [m for m in metrics if m.metric_name == "net_income"]
    assert [m.mapping_priority for m in net_income] == [0, 1]


def _silver_row(
    *,
    doc_id: str,
    metric_name: str = "revenue",
    period_key: str = "CurrentYear",
    scope: str = "consolidated",
    priority: int = 0,
    value: str = "100",
    row_number: int = 2,
):
    return {
        "doc_id": doc_id,
        "metric_name": metric_name,
        "accounting_standard": "jp_gaap",
        "mapping_version": "2026-10-14-v1",
        "mapping_priority": priority,
        "scope": scope,
        "period_key": period_key,
        "element_id": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
        "item_name": "売上高",
        "context_id": f"{period_key}Duration" + ("_NonConsolidatedMember" if scope == "individual" else ""),
        "relative_year": "当期" if period_key == "CurrentYear" else "前期",
        "period_type": "期間",
        "unit_id": "JPY",
        "unit_label": "円",
        "raw_value": value,
        "numeric_value": Decimal(value),
        "source_file": "XBRL_TO_CSV/test.csv",
        "row_number": row_number,
    }


def test_runtime_selection_prefers_newer_filing_then_consolidated_then_priority():
    rows = [
        _silver_row(doc_id="OLD", value="100", row_number=2),
        _silver_row(doc_id="NEW", scope="individual", value="105", row_number=3),
        _silver_row(doc_id="NEW", scope="consolidated", priority=1, value="108", row_number=4),
        _silver_row(doc_id="NEW", scope="consolidated", priority=0, value="110", row_number=5),
    ]

    selected = select_runtime_metrics(rows, ["NEW", "OLD"])
    current = selected["revenue"]["CurrentYear"]

    assert current["numeric_value"] == "110"
    assert current["doc_id"] == "NEW"
    assert current["scope"] == "consolidated"
    assert current["row_number"] == 5
    assert current["element_id"] == "jpcrp_cor:NetSalesSummaryOfBusinessResults"
