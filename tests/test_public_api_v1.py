from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def _research_payload() -> dict:
    return {
        "symbol": "AAPL",
        "market": "NASDAQ",
        "currency": "USD",
        "region": "US",
        "question": "Analyze Apple as an investment over the next 12 months",
        "horizon": "12m",
        "modules": ["fundamental", "valuation", "risk", "market"],
        "as_of": "2026-10-15T02:30:00Z",
    }


def test_public_health_hides_internal_readiness_details():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Investment Research API"
    assert set(body) == {"status", "service", "version"}


def test_public_metadata_describes_only_stable_entrypoints():
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["links"]["public_api_base"] == "/api/v1"
    assert body["links"]["meta"] == "/api/v1/meta"
    assert body["endpoints"]["research"] == "POST /api/v1/research"
    assert body["endpoints"]["valuation_dcf"] == "POST /api/v1/valuation/dcf"
    assert body["compatibility"]["legacy_base"] == "/v1"


def test_public_route_uses_stable_counts_not_internal_agent_ids():
    response = client.post("/api/v1/route", json=_research_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["selected"] == body["specialists"] + body["verifiers"] + body["critics"] + body["cio"]
    assert body["cio"] == 1
    assert "A100" not in response.text
    assert "selected_agents" not in body


def test_public_validation_uses_stable_error_code():
    response = client.post(
        "/api/v1/route",
        json={"symbol": "", "question": "", "modules": []},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["request_id"]


def test_public_dcf_wraps_deterministic_valuation():
    response = client.post(
        "/api/v1/valuation/dcf",
        json={
            "symbol": "TEST",
            "market": "NYSE",
            "currency": "USD",
            "as_of": "2026-10-15T02:30:00Z",
            "cash_flow_basis": "fcff",
            "base_fcf": "100",
            "growth_rates": ["0.10", "0.08", "0.06"],
            "discount_rate": "0.10",
            "terminal_growth": "0.02",
            "net_debt": "20",
            "diluted_shares": "10",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["symbol"] == "TEST"
    assert body["cash_flow_basis"] == "fcff"
    assert body["value_basis"] == "enterprise"
    assert Decimal(body["implied_value_per_share"]) == Decimal("147.9090909090909090909090909090909")


def test_public_research_maps_missing_database_to_public_error_contract():
    response = client.post("/api/v1/research", json=_research_payload())
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DATA_UNAVAILABLE"
    assert "DATABASE_URL" in body["error"]["message"]


def test_openapi_exposes_public_facade_and_retains_legacy_compatibility():
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/research" in paths
    assert "/api/v1/route" in paths
    assert "/api/v1/valuation/dcf" in paths
    assert "/api/v1/reports/{request_id}" in paths
    assert "/v1/research" in paths
