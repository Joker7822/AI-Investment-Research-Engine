from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi.testclient import TestClient

from app.main import _quota_exhausted, app


def test_root_serves_browser_research_app():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    body = response.text
    assert "Investment Research" in body
    assert "AI投資分析を実行" in body
    assert "'/api/v1/research'" in body
    assert "'/api/v1/route'" in body
    assert "'/api/v1/valuation/dcf'" in body
    assert "sessionStorage" in body
    assert "localStorage" not in body

    head = client.head("/")
    assert head.status_code == 200


def test_root_web_app_preserves_mobile_ux_contract():
    client = TestClient(app)
    body = client.get("/").text

    assert "viewport-fit=cover" in body
    assert "env(safe-area-inset-bottom)" in body
    assert "env(safe-area-inset-left)" in body
    assert "@media (pointer:coarse)" in body
    assert "scroll-snap-type:x mandatory" in body
    assert "font-size:16px" in body
    assert 'id="resultPanel"' in body
    assert "revealResultOnMobile" in body
    assert "scrollIntoView({behavior:'smooth', block:'start'})" in body


def test_machine_readable_metadata_prefers_public_api_entrypoints():
    client = TestClient(app)
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["links"]["web_app"] == "/"
    assert body["links"]["health"] == "/api/v1/health"
    assert body["links"]["meta"] == "/api/v1/meta"
    assert body["links"]["public_api_base"] == "/api/v1"
    assert body["links"]["readiness"] == "/v1/ready"
    assert body["links"]["docs"] == "/docs"
    assert body["endpoints"]["research"] == "POST /api/v1/research"
    assert body["endpoints"]["valuation_dcf"] == "POST /api/v1/valuation/dcf"
    assert body["compatibility"]["legacy_base"] == "/v1"


def test_openai_quota_exhaustion_is_detected_without_exposing_provider_payload():
    error = SimpleNamespace(code="credit_balance_exhausted", body={})
    assert _quota_exhausted(error) is True
    limited = SimpleNamespace(code="rate_limit_exceeded", body={})
    assert _quota_exhausted(limited) is False


def test_readiness_fails_closed_without_production_secrets():
    client = TestClient(app)
    response = client.get("/v1/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] is False
    assert body["checks"]["openai_configured"] is False
    assert body["checks"]["api_auth_configured"] is False


def test_render_blueprint_wires_production_dependencies():
    blueprint = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))

    web = next(service for service in blueprint["services"] if service["type"] == "web")
    assert web["runtime"] == "docker"
    assert web["region"] == "singapore"
    assert web["healthCheckPath"] == "/v1/ready"
    assert web["preDeployCommand"] == "python -m app.jobs.migrate_db"
    assert web["autoDeployTrigger"] == "checksPass"
    assert web["maxShutdownDelaySeconds"] >= 120

    env = {item["key"]: item for item in web["envVars"]}
    assert env["DATABASE_URL"]["fromDatabase"]["name"] == "investment-research-db"
    assert env["OPENAI_API_KEY"]["sync"] is False
    assert env["CUSTOM_LLM_BASE_URL"]["sync"] is False
    assert env["CUSTOM_LLM_API_KEY"]["sync"] is False
    assert env["CUSTOM_LLM_MODEL"]["sync"] is False
    assert env["CUSTOM_LLM_TIMEOUT_SECONDS"]["value"] == "90"
    assert env["CUSTOM_LLM_FAILOVER_COOLDOWN_SECONDS"]["value"] == "60"
    assert env["PUBLIC_API_KEY_HASHES"]["sync"] is False
    assert env["STRICT_STARTUP_READINESS"]["value"] == "true"
    assert env["API_RESEARCH_RATE_LIMIT_PER_MINUTE"]["value"] == "3"
    assert env["API_RESEARCH_DAILY_REQUEST_QUOTA"]["value"] == "24"
    assert env["API_RESEARCH_DAILY_AGENT_QUOTA"]["value"] == "600"
    assert env["API_RESEARCH_MAX_AGENTS_PER_REQUEST"]["value"] == "36"
    assert env["WEB_CONCURRENCY"]["value"] == "1"

    cron = next(service for service in blueprint["services"] if service["type"] == "cron")
    assert cron["region"] == "singapore"
    assert cron["schedule"] == "30 9 * * *"
    assert cron["dockerCommand"] == "edinet-sync"

    database = blueprint["databases"][0]
    assert database["name"] == "investment-research-db"
    assert database["region"] == "singapore"
    assert database["ipAllowList"] == []


def test_production_dockerfile_contains_runtime_assets_and_nonroot_user():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY app /app/app" in dockerfile
    assert "COPY db /app/db" in dockerfile
    assert "COPY scripts /app/scripts" in dockerfile
    assert "USER app" in dockerfile
    assert "./scripts/start-api.sh" in dockerfile
