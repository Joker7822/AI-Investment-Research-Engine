from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.domain.schemas import AnalysisRequest, EvidenceRef
from app.providers.base import ResearchDataProvider


BASE_URL = "https://api.massive.com"
NEW_YORK = ZoneInfo("America/New_York")
MARKET_CLOSE_BUFFER = time(16, 15)
US_EXCHANGES = {"NASDAQ", "NYSE", "NYSEARCA", "AMEX", "ARCA"}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def latest_complete_market_date(as_of: datetime) -> date:
    """Return the latest US session date that is safe to treat as complete.

    We intentionally use a small post-close buffer. Holidays/weekends need no
    special calendar here because the aggregates endpoint simply returns no bar.
    """

    local = _aware(as_of).astimezone(NEW_YORK)
    if local.timetz().replace(tzinfo=None) >= MARKET_CLOSE_BUFFER:
        return local.date()
    return local.date() - timedelta(days=1)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _market_close_observed_at(session_date: date) -> datetime:
    return datetime.combine(session_date, MARKET_CLOSE_BUFFER, tzinfo=NEW_YORK)


def _bar_session_date(bar: dict[str, Any]) -> date | None:
    timestamp_ms = bar.get("t")
    if timestamp_ms is None:
        return None
    try:
        stamp = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return stamp.astimezone(NEW_YORK).date()


def _safe_bars(payload: dict[str, Any], cutoff: date) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for raw in payload.get("results", []) or []:
        session_date = _bar_session_date(raw)
        if session_date is None or session_date > cutoff:
            continue
        bars.append(
            {
                "date": session_date.isoformat(),
                "open": raw.get("o"),
                "high": raw.get("h"),
                "low": raw.get("l"),
                "close": raw.get("c"),
                "volume": raw.get("v"),
                "vwap": raw.get("vw"),
                "transactions": raw.get("n"),
            }
        )
    bars.sort(key=lambda row: row["date"])
    return bars


def _safe_dividends(payload: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    """Keep dividends only once they were publicly declared.

    The ex-date can be after the analysis date; an announced future dividend is
    legitimately knowable. If declaration_date is unavailable, we conservatively
    require the ex-date itself to be on/before the analysis date.
    """

    cutoff = _aware(as_of).astimezone(NEW_YORK).date()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("results", []) or []:
        declaration = _parse_date(raw.get("declaration_date"))
        ex_date = _parse_date(raw.get("ex_dividend_date"))
        known_date = declaration or ex_date
        if known_date is None or known_date > cutoff:
            continue
        rows.append(
            {
                "id": raw.get("id"),
                "declaration_date": raw.get("declaration_date"),
                "ex_dividend_date": raw.get("ex_dividend_date"),
                "record_date": raw.get("record_date"),
                "pay_date": raw.get("pay_date"),
                "cash_amount": raw.get("cash_amount"),
                "split_adjusted_cash_amount": raw.get("split_adjusted_cash_amount"),
                "currency": raw.get("currency"),
                "frequency": raw.get("frequency"),
                "distribution_type": raw.get("distribution_type"),
                "known_date": known_date.isoformat(),
            }
        )
    rows.sort(key=lambda row: row["known_date"])
    return rows


def _safe_splits(payload: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    """Keep only already-effective splits.

    Massive's split record does not expose an announcement timestamp. Including a
    future execution date during historical analysis could leak knowledge that was
    not yet available, so the conservative rule is execution_date <= as_of.
    """

    cutoff = _aware(as_of).astimezone(NEW_YORK).date()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("results", []) or []:
        execution = _parse_date(raw.get("execution_date"))
        if execution is None or execution > cutoff:
            continue
        rows.append(
            {
                "id": raw.get("id"),
                "execution_date": execution.isoformat(),
                "split_from": raw.get("split_from"),
                "split_to": raw.get("split_to"),
                "adjustment_type": raw.get("adjustment_type"),
            }
        )
    rows.sort(key=lambda row: row["execution_date"])
    return rows


class MassiveMarketDataProvider(ResearchDataProvider):
    """Enrich US equities/ETFs with as-of-safe raw OHLCV and corporate actions.

    Historical aggregate bars are deliberately requested with ``adjusted=false``.
    Vendor-adjusted history can be restated after a later split/dividend and is
    therefore unsafe for point-in-time backtests unless the adjustment process is
    reconstructed using only actions knowable at the requested ``as_of``.
    """

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 15.0,
        lookback_days: int = 400,
        client: httpx.AsyncClient | None = None,
    ):
        if not api_key.strip():
            raise ValueError("Massive API key is required")
        if lookback_days < 30:
            raise ValueError("lookback_days must be at least 30")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.lookback_days = lookback_days
        self._client = client

    @staticmethod
    def _supports(request: AnalysisRequest) -> bool:
        if request.asset.type not in {"equity", "etf"}:
            return False
        region = (request.asset.region or "").lower()
        exchange = (request.asset.exchange or "").upper()
        return region in {
            "us",
            "usa",
            "united states",
            "united states of america",
        } or exchange in US_EXCHANGES

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["apiKey"] = self.api_key
        url = f"{BASE_URL}{path}"
        if self._client is not None:
            response = await self._client.get(url, params=query, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=query, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        if not self._supports(request):
            return request

        ticker = request.asset.ticker.upper()
        cutoff = latest_complete_market_date(request.as_of)
        start = cutoff - timedelta(days=self.lookback_days)

        bars_payload = await self._get_json(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{cutoff.isoformat()}",
            {
                "adjusted": "false",
                "sort": "asc",
                "limit": 50000,
            },
        )
        dividends_payload = await self._get_json(
            "/stocks/v1/dividends",
            {"ticker": ticker, "limit": 1000, "sort": "ex_dividend_date.desc"},
        )
        splits_payload = await self._get_json(
            "/stocks/v1/splits",
            {"ticker": ticker, "limit": 1000, "sort": "execution_date.desc"},
        )

        bars = _safe_bars(bars_payload, cutoff)
        dividends = _safe_dividends(dividends_payload, request.as_of)
        splits = _safe_splits(splits_payload, request.as_of)

        market_data = dict(request.market_data)
        market_data["massive"] = {
            "ticker": ticker,
            "price_adjustment": "unadjusted",
            "latest_complete_market_date": cutoff.isoformat(),
            "bars": bars,
            "dividends": dividends,
            "splits": splits,
        }

        evidence = list(request.evidence)
        existing_ids = {item.source_id for item in evidence}

        if bars:
            latest = bars[-1]
            source_id = f"massive:ohlcv:{ticker}:{latest['date']}"
            if source_id not in existing_ids:
                evidence.append(
                    EvidenceRef(
                        source_id=source_id,
                        claim=(
                            f"{ticker} raw daily OHLCV on {latest['date']}: "
                            f"O={latest['open']} H={latest['high']} L={latest['low']} "
                            f"C={latest['close']} V={latest['volume']}"
                        ),
                        source_type="market_data",
                        observed_at=_market_close_observed_at(date.fromisoformat(latest["date"])),
                        url=(
                            f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/"
                            f"{latest['date']}/{latest['date']}"
                        ),
                        quality=0.9,
                    )
                )
                existing_ids.add(source_id)

        for dividend in dividends:
            event_id = dividend.get("id") or dividend.get("ex_dividend_date")
            source_id = f"massive:dividend:{ticker}:{event_id}"
            if source_id in existing_ids:
                continue
            known_date = date.fromisoformat(dividend["known_date"])
            evidence.append(
                EvidenceRef(
                    source_id=source_id,
                    claim=(
                        f"{ticker} dividend cash_amount={dividend.get('cash_amount')} "
                        f"{dividend.get('currency')}; ex_date={dividend.get('ex_dividend_date')}"
                    ),
                    source_type="corporate_action",
                    observed_at=datetime.combine(known_date, time.min, tzinfo=NEW_YORK),
                    url=f"{BASE_URL}/stocks/v1/dividends",
                    quality=0.9,
                )
            )
            existing_ids.add(source_id)

        for split in splits:
            event_id = split.get("id") or split.get("execution_date")
            source_id = f"massive:split:{ticker}:{event_id}"
            if source_id in existing_ids:
                continue
            execution = date.fromisoformat(split["execution_date"])
            evidence.append(
                EvidenceRef(
                    source_id=source_id,
                    claim=(
                        f"{ticker} split on {split['execution_date']}: "
                        f"{split.get('split_from')} -> {split.get('split_to')} "
                        f"({split.get('adjustment_type')})"
                    ),
                    source_type="corporate_action",
                    observed_at=datetime.combine(execution, time.min, tzinfo=NEW_YORK),
                    url=f"{BASE_URL}/stocks/v1/splits",
                    quality=0.9,
                )
            )
            existing_ids.add(source_id)

        return request.model_copy(update={"market_data": market_data, "evidence": evidence})
