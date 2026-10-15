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


def _dcf_payload() -> dict:
    return {
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
    }


def test_public_health_hides_internal_readiness_details():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Investment Research API"
    assert set(body) == {"status", "service", "version"}


def test_public_ai_status_validates_auth_and_hides_provider_secrets():
    response = client.get("/api/v1/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["openai_configured"] is False
    assert body["custom_configured"] is False
    assert body["custom_model"] is None
    assert "api_key" not in response.text.lower()
    assert "base_url" not in response.text.lower()


def test_public_custom_probe_reports_unconfigured_provider_without_secrets():
    response = client.post("/api/v1/ai/custom/test")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "CUSTOM_API_NOT_CONFIGURED"
    assert "CUSTOM_LLM_API_KEY" not in response.text


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


def test_public_rejects_whitespace_only_required_fields():
    response = client.post(
        "/api/v1/route",
        json={"symbol": "   ", "question": "   ", "horizon": "   ", "modules": ["risk"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_public_dcf_wraps_deterministic_valuation():
    response = client.post("/api/v1/valuation/dcf", json=_dcf_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["symbol"] == "TEST"
    assert body["cash_flow_basis"] == "fcff"
    assert body["value_basis"] == "enterprise"
    assert Decimal(body["implied_value_per_share"]) == Decimal("147.9090909090909090909090909090909")


def test_public_dcf_semantic_validation_stays_json_safe():
    payload = _dcf_payload()
    payload["discount_rate"] = "0.02"
    payload["terminal_growth"] = "0.03"
    response = client.post("/api/v1/valuation/dcf", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["message"] == "DCF assumptions are invalid"
    assert isinstance(body["error"]["details"], list)


def test_public_research_hides_missing_database_implementation_detail():
    response = client.post("/api/v1/research", json=_research_payload())
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DATA_UNAVAILABLE"
    message = body["error"]["message"]
    assert message == "Required data services are temporarily unavailable"
    assert "DATABASE_URL" not in response.text
    assert "OPENAI" not in response.text


def test_openapi_exposes_public_provider_switch_and_legacy_compatibility():
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/ai/status" in paths
    assert "/api/v1/ai/custom/test" in paths
    assert "/api/v1/research" in paths
    assert "/api/v1/route" in paths
    assert "/api/v1/valuation/dcf" in paths
    assert "/api/v1/reports/{request_id}" in paths
    assert "/v1/research" in paths

    parameters = paths["/api/v1/research"]["post"]["parameters"]
    provider = next(item for item in parameters if item["name"] == "X-AI-Provider")
    assert provider["in"] == "header"
    provider_schema = provider["schema"]
    if "$ref" in provider_schema:
        component_name = provider_schema["$ref"].rsplit("/", 1)[-1]
        provider_schema = schema["components"]["schemas"][component_name]
    assert set(provider_schema["enum"]) == {"auto", "openai", "custom"}
    assert provider_schema["default"] == "auto"
