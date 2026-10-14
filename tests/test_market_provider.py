from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from app.domain.schemas import AnalysisRequest, AssetRef
from app.providers.massive import MassiveMarketDataProvider, latest_complete_market_date


TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


def _ms(year: int, month: int, day: int) -> int:
    dt = datetime(year, month, day, 0, 0, tzinfo=NEW_YORK)
    return int(dt.timestamp() * 1000)


def _request(as_of: datetime) -> AnalysisRequest:
    return AnalysisRequest(
        as_of=as_of,
        asset=AssetRef(
            type="equity",
            ticker="AAPL",
            exchange="NASDAQ",
            currency="USD",
            region="US",
        ),
        user_question="Analyze Apple",
    )


def test_latest_complete_market_date_before_us_close_uses_previous_day():
    as_of = datetime(2026, 10, 14, 19, 0, tzinfo=TOKYO)  # 06:00 New York
    assert latest_complete_market_date(as_of).isoformat() == "2026-10-13"


def test_latest_complete_market_date_after_us_close_uses_same_day():
    as_of = datetime(2026, 10, 15, 7, 0, tzinfo=TOKYO)  # 18:00 New York on Oct 14
    assert latest_complete_market_date(as_of).isoformat() == "2026-10-14"


async def test_provider_filters_future_bars_and_corporate_actions():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        path = request.url.path
        if "/v2/aggs/ticker/" in path:
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        {"t": _ms(2026, 10, 13), "o": 100, "h": 105, "l": 99, "c": 104, "v": 1000},
                        {"t": _ms(2026, 10, 14), "o": 104, "h": 110, "l": 103, "c": 109, "v": 2000},
                    ],
                },
            )
        if path.endswith("/stocks/v1/dividends"):
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        {
                            "id": "known-future-ex",
                            "declaration_date": "2026-10-01",
                            "ex_dividend_date": "2026-10-20",
                            "cash_amount": 0.30,
                            "currency": "USD",
                        },
                        {
                            "id": "not-yet-known",
                            "declaration_date": "2026-10-15",
                            "ex_dividend_date": "2026-11-01",
                            "cash_amount": 0.40,
                            "currency": "USD",
                        },
                    ],
                },
            )
        if path.endswith("/stocks/v1/splits"):
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        {
                            "id": "past-split",
                            "execution_date": "2026-10-01",
                            "split_from": 1,
                            "split_to": 2,
                            "adjustment_type": "forward_split",
                        },
                        {
                            "id": "future-split",
                            "execution_date": "2026-10-20",
                            "split_from": 1,
                            "split_to": 4,
                            "adjustment_type": "forward_split",
                        },
                    ],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = MassiveMarketDataProvider("test-key", client=client)
        result = await provider.enrich(
            _request(datetime(2026, 10, 14, 19, 0, tzinfo=TOKYO))
        )

    data = result.market_data["massive"]
    assert data["price_adjustment"] == "unadjusted"
    assert data["latest_complete_market_date"] == "2026-10-13"
    assert [bar["date"] for bar in data["bars"]] == ["2026-10-13"]
    assert [row["id"] for row in data["dividends"]] == ["known-future-ex"]
    assert [row["id"] for row in data["splits"]] == ["past-split"]

    aggregate_request = next(r for r in seen_requests if "/v2/aggs/ticker/" in r.url.path)
    assert aggregate_request.url.params["adjusted"] == "false"
    assert aggregate_request.url.path.endswith("/2026-10-13")

    source_ids = {item.source_id for item in result.evidence}
    assert "massive:ohlcv:AAPL:2026-10-13" in source_ids
    assert "massive:dividend:AAPL:known-future-ex" in source_ids
    assert "massive:dividend:AAPL:not-yet-known" not in source_ids
    assert "massive:split:AAPL:past-split" in source_ids
    assert "massive:split:AAPL:future-split" not in source_ids


async def test_provider_skips_non_us_assets_without_http_calls():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="7203", exchange="TSE", region="JP"),
        user_question="Analyze Toyota",
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = MassiveMarketDataProvider("test-key", client=client)
        result = await provider.enrich(request)

    assert result is request
    assert called is False
