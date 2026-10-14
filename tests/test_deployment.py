from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.main import app


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
    assert env["PUBLIC_API_KEY_HASHES"]["sync"] is False
    assert env["STRICT_STARTUP_READINESS"]["value"] == "true"
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
    assert "COPY db /app/db" in dockerfile
    assert "COPY scripts /app/scripts" in dockerfile
    assert "USER app" in dockerfile
    assert "./scripts/start-api.sh" in dockerfile
