import logging
from pathlib import Path

from fastapi import Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from openai import RateLimitError

from app.api.auth import configured_api_key_hashes
from app.api.main import app, audit_repository, settings
from app.api.public_v1 import public_router


logger = logging.getLogger(__name__)
_WEB_APP = Path(__file__).resolve().parent / "web" / "index.html"
app.include_router(public_router)


def _metadata_payload() -> dict:
    links = {
        "web_app": "/",
        "health": "/api/v1/health",
        "readiness": "/v1/ready",
        "public_api_base": "/api/v1",
    }
    if settings.api_docs_enabled:
        links["docs"] = "/docs"
        links["redoc"] = "/redoc"
        links["openapi"] = "/openapi.json"

    return {
        "service": settings.api_name,
        "version": settings.api_version,
        "status": "ok",
        "message": "Investment Research API is running",
        "links": links,
        "endpoints": {
            "research": "POST /api/v1/research",
            "valuation_dcf": "POST /api/v1/valuation/dcf",
            "route": "POST /api/v1/route",
            "report": "GET /api/v1/reports/{request_id}",
        },
        "compatibility": {
            "legacy_base": "/v1",
            "status": "supported",
        },
    }


def _web_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
async def web_app() -> HTMLResponse:
    """Serve the same-origin browser console without exposing server-side secrets."""
    return HTMLResponse(
        content=_WEB_APP.read_text(encoding="utf-8"),
        headers=_web_headers(),
    )


@app.head("/", include_in_schema=False)
async def web_app_head() -> Response:
    """Cheap liveness response for browsers and hosting probes."""
    return Response(status_code=status.HTTP_200_OK, headers=_web_headers())


@app.get("/v1/meta", include_in_schema=False)
async def api_metadata() -> JSONResponse:
    """Machine-readable landing metadata retained for API clients."""
    return JSONResponse(content=_metadata_payload())


@app.exception_handler(RequestValidationError)
async def public_aware_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    public_request = request.url.path.startswith("/api/v1/")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "INVALID_REQUEST" if public_request else "validation_error",
                "message": "Request validation failed",
                "request_id": getattr(request.state, "correlation_id", None),
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


def _quota_exhausted(exc: RateLimitError) -> bool:
    codes = {
        str(getattr(exc, "code", "") or "").lower(),
    }
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error", body)
        if isinstance(nested, dict):
            codes.add(str(nested.get("code", "") or "").lower())
            codes.add(str(nested.get("type", "") or "").lower())
    text = str(exc).lower()
    return bool(
        codes.intersection({"credit_balance_exhausted", "insufficient_quota"})
        or "no credits remaining" in text
        or "credit balance" in text and "exhaust" in text
    )


@app.exception_handler(RateLimitError)
async def openai_rate_limit_handler(request: Request, exc: RateLimitError) -> JSONResponse:
    """Expose public capacity errors without leaking provider-specific payloads."""
    if request.url.path.startswith("/api/v1/"):
        code = "AI_CAPACITY_UNAVAILABLE"
        message = "AI analysis is temporarily unavailable"
    else:
        exhausted = _quota_exhausted(exc)
        code = "agent_provider_quota_exhausted" if exhausted else "agent_provider_rate_limited"
        message = (
            "AI analysis is temporarily unavailable because provider credits are exhausted"
            if exhausted
            else "AI analysis is temporarily unavailable because the provider is rate limiting requests"
        )
    logger.warning("OpenAI request unavailable: code=%s", code)
    request_id = getattr(request.state, "correlation_id", None)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": None,
            }
        },
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
