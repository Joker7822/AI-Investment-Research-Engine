from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.auth import build_api_key_dependency, hash_api_key
from app.api.idempotency import (
    hash_idempotency_key,
    research_request_hash,
    validate_idempotency_key,
)
from app.api.main import app
from app.core.settings import Settings
from app.domain.schemas import AnalysisRequest, AssetRef


def test_api_key_dependency_accepts_only_configured_hash():
    secret = "test-secret-key"
    settings = Settings(
        _env_file=None,
        public_api_key_hashes=hash_api_key(secret),
    )
    dependency = build_api_key_dependency(settings)
    test_app = FastAPI()

    @test_app.get("/protected")
    async def protected(_: str = Depends(dependency)):
        return {"ok": True}

    client = TestClient(test_app)
    assert client.get("/protected").status_code == 401
    assert client.get("/protected", headers={"X-API-Key": "wrong-key"}).status_code == 401
    assert client.get("/protected", headers={"X-API-Key": secret}).json() == {"ok": True}


def test_idempotency_hash_ignores_request_id_but_not_payload():
    base = AnalysisRequest(
        request_id="req_one",
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="AAPL", exchange="NASDAQ", region="US"),
        user_question="Analyze Apple",
    )
    same = base.model_copy(update={"request_id": "req_two"})
    changed = base.model_copy(update={"user_question": "Analyze Apple risks"})

    assert research_request_hash(base) == research_request_hash(same)
    assert research_request_hash(base) != research_request_hash(changed)
    assert len(hash_idempotency_key("example-key-123")) == 64


def test_idempotency_key_validation():
    assert validate_idempotency_key("  example-key-123  ") == "example-key-123"
    for invalid in ("short", "x" * 201):
        try:
            validate_idempotency_key(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid idempotency key was accepted")


def test_health_and_openapi_contract():
    client = TestClient(app)
    response = client.get("/v1/health", headers={"X-Request-ID": "test-correlation-id"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-correlation-id"
    body = response.json()
    assert body["status"] == "ok"
    assert body["registered_agents"] == 100

    schema = client.get("/openapi.json").json()
    assert "/v1/research" in schema["paths"]
    assert "/v1/valuation" in schema["paths"]
    assert "/v1/reports/{request_id}" in schema["paths"]
    assert "APIKeyHeader" in schema["components"]["securitySchemes"]


def test_valuation_endpoint_is_deterministic_and_does_not_need_llm():
    client = TestClient(app)
    payload = {
        "as_of": "2026-10-14T12:00:00Z",
        "asset": {
            "type": "equity",
            "ticker": "TEST",
            "exchange": "NYSE",
            "currency": "USD",
            "region": "US"
        },
        "assumptions": {
            "dcf": [
                {
                    "name": "base",
                    "currency": "USD",
                    "base_fcf": "100",
                    "growth_rates": ["0.10", "0.05"],
                    "wacc": "0.10",
                    "terminal_growth": "0.02",
                    "net_debt": "0",
                    "diluted_shares": "10"
                }
            ],
            "multiples": []
        }
    }
    response = client.post("/v1/valuation", json=payload)
    assert response.status_code == 200
    body = response.json()
    result = body["calculations"]["dcf"][0]
    assert result["method"] == "dcf_gordon_growth"
    assert result["classification"] == "assumption_based_calculation_not_evidence"
    assert result["scenario"] == "base"


def test_valuation_endpoint_rejects_empty_scenarios_with_standard_error():
    client = TestClient(app)
    response = client.post(
        "/v1/valuation",
        json={
            "as_of": "2026-10-14T12:00:00Z",
            "asset": {"type": "equity", "ticker": "TEST"},
            "assumptions": {"dcf": [], "multiples": []},
        },
        headers={"X-Request-ID": "validation-test"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"] == "validation-test"


def test_public_research_requires_database_in_default_test_environment():
    client = TestClient(app)
    response = client.post(
        "/v1/research",
        json={
            "as_of": "2026-10-14T12:00:00Z",
            "asset": {"type": "equity", "ticker": "AAPL", "region": "US"},
            "user_question": "Analyze Apple"
        },
        headers={"Idempotency-Key": "research-test-123"},
    )
    # CI has no DATABASE_URL; the public research contract fails closed.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_database_required"
