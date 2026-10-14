from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from app.domain.schemas import AnalysisRequest, EvidenceRef
from app.providers.base import ResearchDataProvider


DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOCUMENT_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
TOKYO = ZoneInfo("Asia/Tokyo")
JP_EXCHANGES = {"TSE", "TYO", "TOKYO", "JPX"}
PERIOD_ORDER = {
    "CurrentYear": 0,
    "Prior1Year": 1,
    "Prior2Year": 2,
    "Prior3Year": 3,
    "Prior4Year": 4,
}


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
    """Small EDINET API v2 client used by ingestion jobs."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        max_download_bytes: int = 128 * 1024 * 1024,
    ):
        if not api_key.strip():
            raise ValueError("EDINET API key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client
        self.max_download_bytes = max_download_bytes

    async def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            response = await self._client.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response

    async def fetch_document_list(self, target_date: date) -> list[dict[str, Any]]:
        response = await self._get(
            DOCUMENTS_URL,
            {"date": target_date.isoformat(), "type": 2, "Subscription-Key": self.api_key},
        )
        normalized: list[dict[str, Any]] = []
        for row in (response.json().get("results", []) or []):
            item = normalize_document(row, target_date)
            if item is not None:
                normalized.append(item)
        return normalized

    async def fetch_csv_package(self, doc_id: str) -> bytes:
        response = await self._get(
            DOCUMENT_URL.format(doc_id=doc_id),
            {"type": 5, "Subscription-Key": self.api_key},
        )
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_download_bytes:
            raise ValueError("EDINET CSV package exceeds download-size limit")
        payload = response.content
        if len(payload) > self.max_download_bytes:
            raise ValueError("EDINET CSV package exceeds download-size limit")
        if not payload.startswith(b"PK"):
            raise ValueError("EDINET type=5 response is not a ZIP archive")
        return payload


class EdinetFilingIndex(Protocol):
    async def latest_for_sec_code(self, sec_code: str, as_of: datetime, limit: int) -> list[dict[str, Any]]: ...
    async def silver_metrics_for_docs(self, doc_ids: list[str]) -> list[dict[str, Any]]: ...


def _serializable_number(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def select_runtime_metrics(rows: list[dict[str, Any]], doc_ids: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Pick one provenance-preserving row per metric/annual period.

    Newer filings win first (so amendments supersede originals), then consolidated
    scope, then the explicit element priority in the mapping file.
    """
    doc_rank = {doc_id: idx for idx, doc_id in enumerate(doc_ids)}
    candidates = [r for r in rows if r.get("period_key") in PERIOD_ORDER]
    candidates.sort(key=lambda r: (
        doc_rank.get(str(r.get("doc_id")), 10_000),
        str(r.get("metric_name")),
        PERIOD_ORDER.get(str(r.get("period_key")), 99),
        0 if r.get("scope") == "consolidated" else 1,
        int(r.get("mapping_priority") or 0),
        str(r.get("context_id") or ""),
    ))

    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in candidates:
        key = (str(row["metric_name"]), str(row["period_key"]))
        selected.setdefault(key, row)

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for (metric_name, period_key), row in selected.items():
        result.setdefault(metric_name, {})[period_key] = {
            "numeric_value": _serializable_number(row.get("numeric_value")),
            "raw_value": row.get("raw_value"),
            "unit_id": row.get("unit_id"),
            "unit_label": row.get("unit_label"),
            "scope": row.get("scope"),
            "accounting_standard": row.get("accounting_standard"),
            "mapping_version": row.get("mapping_version"),
            "element_id": row.get("element_id"),
            "context_id": row.get("context_id"),
            "relative_year": row.get("relative_year"),
            "doc_id": row.get("doc_id"),
            "source_file": row.get("source_file"),
            "row_number": row.get("row_number"),
        }
    return result


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
        as_of = parse_edinet_datetime(request.as_of)
        if as_of is None:
            raise ValueError("EDINET enrichment requires a valid as_of timestamp")

        sec_code = normalize_sec_code(request.asset.ticker)
        rows = await self.index.latest_for_sec_code(sec_code, as_of, self.limit)
        safe_rows = []
        for row in rows:
            submitted = parse_edinet_datetime(row.get("submitted_at"))
            if submitted is not None and submitted <= as_of:
                safe_rows.append(row)

        filings = list(request.filings)
        existing_doc_ids = {str(item.get("doc_id")) for item in filings if item.get("doc_id")}
        evidence = list(request.evidence)
        existing_source_ids = {item.source_id for item in evidence}
        normalized_for_request = []

        for row in safe_rows:
            doc_id = str(row["doc_id"])
            submitted = parse_edinet_datetime(row.get("submitted_at"))
            item = {
                "provider": "edinet", "doc_id": doc_id,
                "filing_date": str(row.get("filing_date")) if row.get("filing_date") else None,
                "submitted_at": submitted.isoformat() if submitted else None,
                "edinet_code": row.get("edinet_code"), "sec_code": row.get("sec_code"),
                "filer_name": row.get("filer_name"), "ordinance_code": row.get("ordinance_code"),
                "form_code": row.get("form_code"), "doc_type_code": row.get("doc_type_code"),
                "period_start": str(row.get("period_start")) if row.get("period_start") else None,
                "period_end": str(row.get("period_end")) if row.get("period_end") else None,
                "xbrl_flag": bool(row.get("xbrl_flag")), "csv_flag": bool(row.get("csv_flag")),
                "pdf_flag": bool(row.get("pdf_flag")), "legal_status": row.get("legal_status"),
            }
            normalized_for_request.append(item)
            if doc_id not in existing_doc_ids:
                filings.append(item)
                existing_doc_ids.add(doc_id)
            source_id = f"edinet:{doc_id}"
            if source_id not in existing_source_ids:
                evidence.append(EvidenceRef(
                    source_id=source_id,
                    claim=(f"EDINET filing {doc_id}; filer={row.get('filer_name')}; "
                           f"doc_type={row.get('doc_type_code')}; period_end={row.get('period_end')}; "
                           f"submitted_at={submitted.isoformat() if submitted else None}"),
                    source_type="edinet_filing", observed_at=submitted,
                    url=DOCUMENT_URL.format(doc_id=doc_id), quality=1.0,
                ))
                existing_source_ids.add(source_id)

        doc_ids = [str(row["doc_id"]) for row in safe_rows]
        metric_loader = getattr(self.index, "silver_metrics_for_docs", None)
        silver_rows = await metric_loader(doc_ids) if metric_loader is not None and doc_ids else []
        metrics = select_runtime_metrics(silver_rows, doc_ids)

        fundamental_data = dict(request.fundamental_data)
        fundamental_data["edinet"] = {
            "sec_code": sec_code,
            "filings": normalized_for_request,
            "metrics": metrics,
        }
        return request.model_copy(update={
            "filings": filings, "evidence": evidence, "fundamental_data": fundamental_data
        })
