from fastapi.responses import JSONResponse

from app.api.auth import configured_api_key_hashes
from app.api.main import app, audit_repository, settings


@app.get("/", include_in_schema=False)
async def api_landing() -> JSONResponse:
    """Human-friendly API landing response for browsers and simple uptime checks."""
    links = {
        "health": "/v1/health",
        "readiness": "/v1/ready",
    }
    if settings.api_docs_enabled:
        links["docs"] = "/docs"
        links["redoc"] = "/redoc"
        links["openapi"] = "/openapi.json"

    return JSONResponse(
        content={
            "service": settings.api_name,
            "version": settings.api_version,
            "status": "ok",
            "message": "Investment Research API is running",
            "links": links,
            "endpoints": {
                "research": "POST /v1/research",
                "valuation": "POST /v1/valuation",
                "route": "POST /v1/route",
                "report": "GET /v1/reports/{request_id}",
            },
        }
    )


@app.get("/v1/ready", include_in_schema=False)
async def readiness() -> JSONResponse:
    """Production readiness probe without exposing secret values."""
    try:
        auth_configured = bool(configured_api_key_hashes(settings))
    except ValueError:
        auth_configured = False

    database_ready = False
    if settings.database_url:
        try:
            # Reuse the audit repository pool and execute a harmless indexed lookup.
            await audit_repository.get_report("__readiness_probe__")
            database_ready = True
        except Exception:
            database_ready = False

    checks = {
        "database": database_ready,
        "openai_configured": bool(settings.openai_api_key),
        "api_auth_configured": auth_configured,
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


__all__ = ["app"]
