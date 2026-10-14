from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from app.domain.schemas import AnalysisRequest, EvidenceRef
from app.providers.base import ResearchDataProvider


DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOCUMENT_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
TOKYO = ZoneInfo("Asia/Tokyo")
JP_EXCHANGES = {"TSE", "TYO", "TOKYO", "JPX"}


def normalize_sec_code(ticker: str) -> str:
    value = ticker.upper().strip()
    if value.endswith(".T"):
        value = value[:-2]
    value = value.replace("-", "")
    if len(value) == 4:
        return value + "0"
    return value


def parse_edinet_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TOKYO)
        return value
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TOKYO)
    return parsed


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _flag(value: Any) -> bool:
    return str(value) == "1" or value is True


def normalize_document(row: dict[str, Any], filing_date: date) -> dict[str, Any] | None:
    doc_id = row.get("docID")
    if not doc_id:
        return None
    return {
        "doc_id": str(doc_id),
        "filing_date": filing_date,
        "submitted_at": parse_edinet_datetime(row.get("submitDateTime")),
        "edinet_code": row.get("edinetCode"),
        "sec_code": row.get("secCode"),
        "jcn": row.get("JCN"),
        "filer_name": row.get("filerName"),
        "ordinance_code": row.get("ordinanceCode"),
        "form_code": row.get("formCode"),
        "doc_type_code": row.get("docTypeCode"),
        "period_start": _parse_date(row.get("periodStart")),
        "period_end": _parse_date(row.get("periodEnd")),
        "xbrl_flag": _flag(row.get("xbrlFlag")),
        "csv_flag": _flag(row.get("csvFlag")),
        "pdf_flag": _flag(row.get("pdfFlag")),
        "legal_status": row.get("legalStatus"),
        "raw_json": row,
    }


class EdinetApiClient:
    """Small EDINET API v2 client used by the daily ingestion job."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ):
        if not api_key.strip():
            raise ValueError("EDINET API key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def fetch_document_list(self, target_date: date) -> list[dict[str, Any]]:
        params = {
            "date": target_date.isoformat(),
            "type": 2,
            "Subscription-Key": self.api_key,
        }
        if self._client is not None:
            response = await self._client.get(
                DOCUMENTS_URL,
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        else:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    DOCUMENTS_URL,
                    params=params,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()

        results = payload.get("results", []) or []
        normalized: list[dict[str, Any]] = []
        for row in results:
            item = normalize_document(row, target_date)
            if item is not None:
                normalized.append(item)
        return normalized


class EdinetFilingIndex(Protocol):
    async def latest_for_sec_code(
        self,
        sec_code: str,
        as_of: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class EdinetIndexedProvider(ResearchDataProvider):
    """Enrich Japanese equities from a pre-synced local EDINET filing index."""

    def __init__(self, index: EdinetFilingIndex, limit: int = 8):
        if limit < 1:
            raise ValueError("EDINET index limit must be positive")
        self.index = index
        self.limit = limit

    @staticmethod
    def _supports(request: AnalysisRequest) -> bool:
        if request.asset.type != "equity":
            return False
        region = (request.asset.region or "").lower()
        exchange = (request.asset.exchange or "").upper()
        return region in {"jp", "japan", "jpn"} or exchange in JP_EXCHANGES

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        if not self._supports(request):
            return request

        sec_code = normalize_sec_code(request.asset.ticker)
        rows = await self.index.latest_for_sec_code(sec_code, request.as_of, self.limit)

        # Defense in depth: never trust the storage query alone for point-in-time safety.
        safe_rows: list[dict[str, Any]] = []
        for row in rows:
            submitted = parse_edinet_datetime(row.get("submitted_at"))
            if submitted is None or submitted > request.as_of:
                continue
            safe_rows.append(row)

        filings = list(request.filings)
        existing_doc_ids = {str(item.get("doc_id")) for item in filings if item.get("doc_id")}
        evidence = list(request.evidence)
        existing_source_ids = {item.source_id for item in evidence}

        normalized_for_request: list[dict[str, Any]] = []
        for row in safe_rows:
            doc_id = str(row["doc_id"])
            submitted = parse_edinet_datetime(row.get("submitted_at"))
            item = {
                "provider": "edinet",
                "doc_id": doc_id,
                "filing_date": str(row.get("filing_date")) if row.get("filing_date") else None,
                "submitted_at": submitted.isoformat() if submitted else None,
                "edinet_code": row.get("edinet_code"),
                "sec_code": row.get("sec_code"),
                "filer_name": row.get("filer_name"),
                "ordinance_code": row.get("ordinance_code"),
                "form_code": row.get("form_code"),
                "doc_type_code": row.get("doc_type_code"),
                "period_start": str(row.get("period_start")) if row.get("period_start") else None,
                "period_end": str(row.get("period_end")) if row.get("period_end") else None,
                "xbrl_flag": bool(row.get("xbrl_flag")),
                "csv_flag": bool(row.get("csv_flag")),
                "pdf_flag": bool(row.get("pdf_flag")),
                "legal_status": row.get("legal_status"),
            }
            normalized_for_request.append(item)
            if doc_id not in existing_doc_ids:
                filings.append(item)
                existing_doc_ids.add(doc_id)

            source_id = f"edinet:{doc_id}"
            if source_id not in existing_source_ids:
                evidence.append(
                    EvidenceRef(
                        source_id=source_id,
                        claim=(
                            f"EDINET filing {doc_id}; filer={row.get('filer_name')}; "
                            f"doc_type={row.get('doc_type_code')}; "
                            f"period_end={row.get('period_end')}; submitted_at={submitted.isoformat() if submitted else None}"
                        ),
                        source_type="edinet_filing",
                        observed_at=submitted,
                        url=DOCUMENT_URL.format(doc_id=doc_id),
                        quality=1.0,
                    )
                )
                existing_source_ids.add(source_id)

        fundamental_data = dict(request.fundamental_data)
        fundamental_data["edinet"] = {
            "sec_code": sec_code,
            "filings": normalized_for_request,
        }

        return request.model_copy(
            update={
                "filings": filings,
                "evidence": evidence,
                "fundamental_data": fundamental_data,
            }
        )
