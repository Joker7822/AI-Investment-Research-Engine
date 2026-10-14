from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.domain.schemas import AnalysisRequest, EvidenceRef
from app.providers.base import ResearchDataProvider


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

METRIC_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "assets": ("Assets",),
    "liabilities": ("Liabilities", "LiabilitiesAndStockholdersEquity"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
}

ALLOWED_FORMS = {"10-K", "10-Q", "20-F", "40-F"}


def _filing_url(cik: str, accession: str | None) -> str:
    if not accession:
        return COMPANYFACTS_URL.format(cik=cik)
    compact = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}/{accession}-index.html"


def _parse_filed(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _choose_latest_unit_fact(
    units: dict[str, list[dict[str, Any]]], as_of: datetime
) -> tuple[str, dict[str, Any]] | None:
    candidates: list[tuple[datetime, datetime, str, dict[str, Any]]] = []
    preferred_units = [u for u in ("USD", "shares", "USD/shares") if u in units]
    unit_order = preferred_units + [u for u in units if u not in preferred_units]

    for unit in unit_order:
        for fact in units.get(unit, []):
            filed = _parse_filed(fact.get("filed"))
            if filed is None or filed > as_of.astimezone(timezone.utc):
                continue
            if fact.get("form") not in ALLOWED_FORMS:
                continue
            end = _parse_filed(fact.get("end")) or filed
            candidates.append((filed, end, unit, fact))

    if not candidates:
        return None
    _, _, unit, fact = max(candidates, key=lambda row: (row[0], row[1]))
    return unit, fact


def extract_metrics(
    companyfacts: dict[str, Any], as_of: datetime, cik: str
) -> tuple[dict[str, Any], list[EvidenceRef]]:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    metrics: dict[str, Any] = {}
    evidence: list[EvidenceRef] = []

    for metric, tags in METRIC_TAGS.items():
        selected: tuple[str, str, dict[str, Any]] | None = None
        for tag in tags:
            node = us_gaap.get(tag)
            if not node:
                continue
            chosen = _choose_latest_unit_fact(node.get("units", {}), as_of)
            if chosen is not None:
                unit, fact = chosen
                selected = (tag, unit, fact)
                break
        if selected is None:
            continue

        tag, unit, fact = selected
        accession = fact.get("accn")
        source_id = f"sec:{cik}:{accession or fact.get('filed')}:{tag}"
        filed = _parse_filed(fact.get("filed"))
        metrics[metric] = {
            "value": fact.get("val"),
            "unit": unit,
            "period_start": fact.get("start"),
            "period_end": fact.get("end"),
            "filed": fact.get("filed"),
            "form": fact.get("form"),
            "accession": accession,
            "tag": tag,
            "source_id": source_id,
        }
        evidence.append(
            EvidenceRef(
                source_id=source_id,
                claim=(
                    f"{metric}={fact.get('val')} {unit}; "
                    f"period_end={fact.get('end')}; form={fact.get('form')}"
                ),
                source_type="sec_filing",
                observed_at=filed,
                url=_filing_url(cik, accession),
                quality=1.0,
            )
        )

    return metrics, evidence


class SecCompanyFactsProvider(ResearchDataProvider):
    """Enrich US equities with SEC Company Facts while enforcing the request's as-of date."""

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ):
        if not user_agent.strip():
            raise ValueError("SEC user agent must identify the application/contact")
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def _get_json(self, url: str) -> dict[str, Any]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if self._client is not None:
            response = await self._client.get(
                url, headers=headers, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=headers, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            return response.json()

    async def _resolve_cik(self, ticker: str) -> str:
        payload = await self._get_json(TICKERS_URL)
        wanted = ticker.upper()
        for row in payload.values():
            if str(row.get("ticker", "")).upper() == wanted:
                return f"{int(row['cik_str']):010d}"
        raise ValueError(f"SEC CIK not found for ticker {ticker}")

    @staticmethod
    def _is_us_equity(request: AnalysisRequest) -> bool:
        if request.asset.type != "equity":
            return False
        region = (request.asset.region or "").lower()
        exchange = (request.asset.exchange or "").upper()
        return region in {
            "us",
            "usa",
            "united states",
            "united states of america",
        } or exchange in {"NASDAQ", "NYSE", "NYSEARCA", "AMEX"}

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        if not self._is_us_equity(request):
            return request

        cik = await self._resolve_cik(request.asset.ticker)
        companyfacts = await self._get_json(COMPANYFACTS_URL.format(cik=cik))
        metrics, new_evidence = extract_metrics(companyfacts, request.as_of, cik)

        fundamental = dict(request.fundamental_data)
        fundamental["sec"] = {
            "cik": cik,
            "entity_name": companyfacts.get("entityName"),
            "metrics": metrics,
        }

        existing_ids = {e.source_id for e in request.evidence}
        evidence = list(request.evidence)
        evidence.extend(e for e in new_evidence if e.source_id not in existing_ids)

        filings = list(request.filings)
        filings.extend(metrics.values())

        return request.model_copy(
            update={
                "fundamental_data": fundamental,
                "evidence": evidence,
                "filings": filings,
            }
        )
