from fastapi.responses import JSONResponse

from app.api.auth import configured_api_key_hashes
from app.api.main import app, audit_repository, settings


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
