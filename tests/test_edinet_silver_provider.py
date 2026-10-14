from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.schemas import AnalysisRequest, AssetRef
from app.providers.edinet import EdinetIndexedProvider


TOKYO = ZoneInfo("Asia/Tokyo")


class SilverIndex:
    def __init__(self):
        self.metric_doc_ids = None

    async def latest_for_sec_code(self, sec_code, as_of, limit):
        assert sec_code == "72030"
        return [
            {
                "doc_id": "S100NEW",
                "filing_date": date(2026, 6, 24),
                "submitted_at": datetime(2026, 6, 24, 15, 10, tzinfo=TOKYO),
                "edinet_code": "E02144",
                "sec_code": "72030",
                "filer_name": "Toyota Motor Corporation",
                "doc_type_code": "120",
                "period_start": date(2025, 4, 1),
                "period_end": date(2026, 3, 31),
                "xbrl_flag": True,
                "csv_flag": True,
                "pdf_flag": True,
                "legal_status": "1",
            },
            {
                "doc_id": "S100OLD",
                "filing_date": date(2025, 6, 20),
                "submitted_at": datetime(2025, 6, 20, 14, 0, tzinfo=TOKYO),
                "edinet_code": "E02144",
                "sec_code": "72030",
                "filer_name": "Toyota Motor Corporation",
                "doc_type_code": "120",
                "period_start": date(2024, 4, 1),
                "period_end": date(2025, 3, 31),
                "xbrl_flag": True,
                "csv_flag": True,
                "pdf_flag": True,
                "legal_status": "1",
            },
        ]

    async def silver_metrics_for_docs(self, doc_ids):
        self.metric_doc_ids = list(doc_ids)
        return [
            {
                "doc_id": "S100OLD",
                "metric_name": "revenue",
                "accounting_standard": "jp_gaap",
                "mapping_version": "2026-10-14-v1",
                "mapping_priority": 0,
                "scope": "consolidated",
                "period_key": "CurrentYear",
                "element_id": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
                "item_name": "売上高",
                "context_id": "CurrentYearDuration",
                "relative_year": "当期",
                "period_type": "期間",
                "unit_id": "JPY",
                "unit_label": "円",
                "raw_value": "40000000000000",
                "numeric_value": Decimal("40000000000000"),
                "source_file": "XBRL_TO_CSV/old.csv",
                "row_number": 10,
            },
            {
                "doc_id": "S100NEW",
                "metric_name": "revenue",
                "accounting_standard": "jp_gaap",
                "mapping_version": "2026-10-14-v1",
                "mapping_priority": 0,
                "scope": "consolidated",
                "period_key": "CurrentYear",
                "element_id": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
                "item_name": "売上高",
                "context_id": "CurrentYearDuration",
                "relative_year": "当期",
                "period_type": "期間",
                "unit_id": "JPY",
                "unit_label": "円",
                "raw_value": "48000000000000",
                "numeric_value": Decimal("48000000000000"),
                "source_file": "XBRL_TO_CSV/new.csv",
                "row_number": 11,
            },
        ]


async def test_provider_places_latest_silver_metric_in_fundamental_data():
    index = SilverIndex()
    provider = EdinetIndexedProvider(index, limit=8)
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, 15, 0, tzinfo=TOKYO),
        asset=AssetRef(type="equity", ticker="7203.T", exchange="TSE", region="JP"),
        user_question="Analyze Toyota",
    )

    result = await provider.enrich(request)

    assert index.metric_doc_ids == ["S100NEW", "S100OLD"]
    revenue = result.fundamental_data["edinet"]["metrics"]["revenue"]["CurrentYear"]
    assert revenue["numeric_value"] == "48000000000000"
    assert revenue["doc_id"] == "S100NEW"
    assert revenue["source_file"] == "XBRL_TO_CSV/new.csv"
    assert revenue["row_number"] == 11
    assert revenue["mapping_version"] == "2026-10-14-v1"
