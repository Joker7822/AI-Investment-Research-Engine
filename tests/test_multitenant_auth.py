import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.api.auth import (
    AuthPrincipal,
    authenticate_api_key,
    configured_api_principals,
    hash_api_key,
    new_client_request_id,
    request_id_belongs_to_client,
)
from app.api.idempotency import hash_idempotency_key, scoped_idempotency_key_hash
from app.api.main import require_api_key
from app.api.public_v1 import require_public_api_key
from app.core.settings import Settings
from app.main import app


def _public_research_payload() -> dict:
    return {
        "symbol": "AAPL",
        "market": "NASDAQ",
        "currency": "USD",
        "region": "US",
        "question": "Analyze Apple",
        "horizon": "12m",
        "modules": ["fundamental", "risk"],
        "as_of": "2026-10-15T03:30:00Z",
    }


def test_explicit_client_mapping_supports_key_rotation():
    key_one = "tenant-key-one"
    key_two = "tenant-key-two"
    settings = Settings(
        _env_file=None,
        public_api_key_hashes=None,
        public_api_client_keys=(
            f"acme={hash_api_key(key_one)},"
            f"acme={hash_api_key(key_two)}"
        ),
    )

    principals = configured_api_principals(settings)
    assert len(principals) == 2
    assert {principal.client_id for principal in principals} == {"acme"}
    assert authenticate_api_key(settings, key_one).client_id == "acme"
    assert authenticate_api_key(settings, key_two).client_id == "acme"


def test_explicit_mapping_rejects_reserved_local_client_id():
    settings = Settings(
        _env_file=None,
        public_api_key_hashes=None,
        public_api_client_keys=f"local-development={hash_api_key('secret')}",
    )
    with pytest.raises(ValueError, match="reserved local-development"):
        configured_api_principals(settings)


def test_legacy_hash_configuration_gets_stable_pseudonymous_client():
    secret = "legacy-production-key"
    settings = Settings(
        _env_file=None,
        public_api_key_hashes=hash_api_key(secret),
        public_api_client_keys=None,
    )

    first = authenticate_api_key(settings, secret)
    second = authenticate_api_key(settings, secret)
    assert first.client_id == second.client_id
    assert first.client_id.startswith("legacy_")
    assert secret not in first.client_id
    assert first.key_hash == hash_api_key(secret)


def test_request_ids_are_bound_to_client_namespace():
    first_id = new_client_request_id("client-a")
    second_id = new_client_request_id("client-b")

    assert request_id_belongs_to_client(first_id, "client-a") is True
    assert request_id_belongs_to_client(first_id, "client-b") is False
    assert request_id_belongs_to_client(second_id, "client-b") is True
    assert request_id_belongs_to_client(second_id, "client-a") is False


def test_legacy_report_id_shapes_cover_prior_research_and_analyze_ids():
    assert api_main._is_legacy_public_request_id("req_" + "a" * 12) is True
    assert api_main._is_legacy_public_request_id("req_" + "b" * 16) is True
    assert api_main._is_legacy_public_request_id("req_" + "c" * 11) is False
    assert api_main._is_legacy_public_request_id("req_" + "d" * 17) is False


def test_idempotency_key_hashes_are_scoped_per_client():
    raw_key = "same-idempotency-key"
    first = scoped_idempotency_key_hash("client-a", raw_key)
    second = scoped_idempotency_key_hash("client-b", raw_key)
    repeated = scoped_idempotency_key_hash("client-a", raw_key)

    assert first != second
    assert first == repeated
    assert len(first) == 64


def test_single_client_cutover_preserves_legacy_idempotency_namespace(monkeypatch):
    principal = AuthPrincipal(client_id="client-a", key_hash="a" * 64)
    normalized_key = "existing-retry-key"

    monkeypatch.setattr(api_main, "_legacy_report_ids_allowed", lambda: True)
    assert api_main._idempotency_key_hash(principal, normalized_key) == hash_idempotency_key(
        normalized_key
    )

    monkeypatch.setattr(api_main, "_legacy_report_ids_allowed", lambda: False)
    assert api_main._idempotency_key_hash(principal, normalized_key) == scoped_idempotency_key_hash(
        principal.client_id,
        normalized_key,
    )


def test_public_report_endpoint_hides_cross_client_report_ids():
    owner = AuthPrincipal(client_id="client-a", key_hash="a" * 64)
    other = AuthPrincipal(client_id="client-b", key_hash="b" * 64)
    request_id = new_client_request_id(owner.client_id)

    app.dependency_overrides[require_public_api_key] = lambda: other
    try:
        response = TestClient(app).get(f"/api/v1/reports/{request_id}")
    finally:
        app.dependency_overrides.pop(require_public_api_key, None)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "REPORT_NOT_FOUND"
    assert body["error"]["message"] == "Report was not found"


def test_legacy_report_endpoint_cannot_bypass_client_isolation():
    owner = AuthPrincipal(client_id="client-a", key_hash="a" * 64)
    other = AuthPrincipal(client_id="client-b", key_hash="b" * 64)
    request_id = new_client_request_id(owner.client_id)

    app.dependency_overrides[require_api_key] = lambda: other
    try:
        response = TestClient(app).get(f"/v1/reports/{request_id}")
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "report_not_found"


def test_public_research_validates_raw_idempotency_key_before_scoping():
    principal = AuthPrincipal(client_id="client-a", key_hash="a" * 64)
    app.dependency_overrides[require_public_api_key] = lambda: principal
    try:
        response = TestClient(app).post(
            "/api/v1/research",
            json=_public_research_payload(),
            headers={"Idempotency-Key": "short"},
        )
    finally:
        app.dependency_overrides.pop(require_public_api_key, None)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["message"] == "Request could not be accepted"
