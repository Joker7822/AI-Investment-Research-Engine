from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.domain.schemas import AnalysisRequest, AssetRef
from app.providers.edinet import (
    EdinetApiClient,
    EdinetIndexedProvider,
    normalize_sec_code,
)


TOKYO = ZoneInfo("Asia/Tokyo")


def test_normalize_sec_code():
    assert normalize_sec_code("7203") == "72030"
    assert normalize_sec_code("7203.T") == "72030"
    assert normalize_sec_code("607A") == "607A0"
    assert normalize_sec_code("72030") == "72030"


async def test_edinet_api_client_uses_v2_query_and_normalizes_documents():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "metadata": {"status": "200"},
                "results": [
                    {
                        "docID": "S100TEST",
                        "edinetCode": "E02144",
                        "secCode": "72030",
                        "JCN": "1180301018771",
                        "filerName": "Toyota Motor Corporation",
                        "ordinanceCode": "010",
                        "formCode": "030000",
                        "docTypeCode": "120",
                        "periodStart": "2025-04-01",
                        "periodEnd": "2026-03-31",
                        "submitDateTime": "2026-06-24 15:10",
                        "xbrlFlag": "1",
                        "csvFlag": "1",
                        "pdfFlag": "1",
                        "legalStatus": "1",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = EdinetApiClient("secret-key", client=http_client)
        rows = await client.fetch_document_list(date(2026, 6, 24))

    assert len(rows) == 1
    assert rows[0]["doc_id"] == "S100TEST"
    assert rows[0]["sec_code"] == "72030"
    assert rows[0]["submitted_at"].tzinfo is not None
    assert rows[0]["xbrl_flag"] is True
    assert rows[0]["csv_flag"] is True

    assert len(seen) == 1
    assert seen[0].url.path == "/api/v2/documents.json"
    assert seen[0].url.params["date"] == "2026-06-24"
    assert seen[0].url.params["type"] == "2"
    assert seen[0].url.params["Subscription-Key"] == "secret-key"


class FakeIndex:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def latest_for_sec_code(self, sec_code, as_of, limit):
        self.calls.append((sec_code, as_of, limit))
        return self.rows


async def test_indexed_provider_rejects_future_filings_defense_in_depth():
    index = FakeIndex(
        [
            {
                "doc_id": "S100SAFE",
                "filing_date": date(2026, 10, 14),
                "submitted_at": datetime(2026, 10, 14, 14, 0, tzinfo=TOKYO),
                "edinet_code": "E02144",
                "sec_code": "72030",
                "filer_name": "Toyota Motor Corporation",
                "ordinance_code": "010",
                "form_code": "030000",
                "doc_type_code": "120",
                "period_start": date(2025, 4, 1),
                "period_end": date(2026, 3, 31),
                "xbrl_flag": True,
                "csv_flag": True,
                "pdf_flag": True,
                "legal_status": "1",
            },
            {
                "doc_id": "S100FUTURE",
                "filing_date": date(2026, 10, 14),
                "submitted_at": datetime(2026, 10, 14, 16, 0, tzinfo=TOKYO),
                "edinet_code": "E02144",
                "sec_code": "72030",
                "filer_name": "Toyota Motor Corporation",
                "doc_type_code": "120",
                "xbrl_flag": True,
                "csv_flag": True,
                "pdf_flag": True,
                "legal_status": "1",
            },
        ]
    )
    provider = EdinetIndexedProvider(index, limit=8)
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, 15, 0, tzinfo=TOKYO),
        asset=AssetRef(type="equity", ticker="7203.T", exchange="TSE", region="JP"),
        user_question="Analyze Toyota",
    )

    result = await provider.enrich(request)

    assert index.calls[0][0] == "72030"
    assert index.calls[0][2] == 8
    assert [row["doc_id"] for row in result.fundamental_data["edinet"]["filings"]] == ["S100SAFE"]
    assert {item.source_id for item in result.evidence} == {"edinet:S100SAFE"}
    assert result.evidence[0].quality == 1.0


async def test_indexed_provider_skips_non_japanese_assets():
    index = FakeIndex([])
    provider = EdinetIndexedProvider(index)
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, 15, 0, tzinfo=TOKYO),
        asset=AssetRef(type="equity", ticker="AAPL", exchange="NASDAQ", region="US"),
        user_question="Analyze Apple",
    )

    result = await provider.enrich(request)
    assert result is request
    assert index.calls == []
