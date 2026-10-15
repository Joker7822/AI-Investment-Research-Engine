from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from agents import set_default_openai_key
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.auth import (
    AuthPrincipal,
    build_api_key_dependency,
    configured_api_key_hashes,
    configured_api_principals,
    new_client_request_id,
    request_id_belongs_to_client,
)
from app.api.idempotency import (
    hash_idempotency_key,
    research_request_hash,
    scoped_idempotency_key_hash,
    validate_idempotency_key,
)
from app.api.models import (
    ApiErrorBody,
    ApiErrorResponse,
    HealthResponse,
    ResearchResponse,
    RouteResponse,
    ValuationRequest,
    ValuationResponse,
)
from app.core.settings import get_settings
from app.domain.schemas import AnalysisRequest, InvestmentReport
from app.orchestration.engine import InvestmentAnalysisEngine
from app.orchestration.registry import AgentRegistry
from app.orchestration.router import DeterministicRouter
from app.persistence.audit import NullAuditRepository, PostgresAuditRepository
from app.persistence.edinet import EdinetIndexRepository
from app.providers.base import CompositeResearchDataProvider
from app.providers.derived import DerivedFinancialsProvider
from app.providers.edinet import EdinetIndexedProvider
from app.providers.massive import MassiveMarketDataProvider
from app.providers.sec import SecCompanyFactsProvider
from app.providers.valuation import ValuationCalculationProvider


logger = logging.getLogger(__name__)
settings = get_settings()
if settings.openai_api_key:
    set_default_openai_key(settings.openai_api_key)
registry = AgentRegistry(settings)
router = DeterministicRouter(registry)
require_api_key = build_api_key_dependency(settings)

edinet_index_repository = (
    EdinetIndexRepository(settings.database_url, settings.database_schema_path)
    if settings.database_url
    else None
)

providers = []
if settings.sec_user_agent:
    providers.append(SecCompanyFactsProvider(settings.sec_user_agent, settings.sec_timeout_seconds))
if settings.massive_api_key:
    providers.append(
        MassiveMarketDataProvider(
            settings.massive_api_key,
            settings.massive_timeout_seconds,
            settings.massive_lookback_days,
        )
    )
if edinet_index_repository is not None:
    providers.append(EdinetIndexedProvider(edinet_index_repository, settings.edinet_index_limit))
providers.append(DerivedFinancialsProvider())
providers.append(ValuationCalculationProvider())
provider = CompositeResearchDataProvider(providers)
engine = InvestmentAnalysisEngine(settings, registry, router, provider)

valuation_providers = []
if settings.massive_api_key:
    valuation_providers.append(
        MassiveMarketDataProvider(
            settings.massive_api_key,
            settings.massive_timeout_seconds,
            settings.massive_lookback_days,
        )
    )
valuation_providers.append(ValuationCalculationProvider())
valuation_provider = CompositeResearchDataProvider(valuation_providers)

audit_repository = (
    PostgresAuditRepository(settings.database_url, settings.database_schema_path)
    if settings.database_url
    else NullAuditRepository()
)


def _legacy_report_ids_allowed() -> bool:
    principals = configured_api_principals(settings)
    return len({principal.client_id for principal in principals}) == 1


def _is_legacy_public_request_id(request_id: str) -> bool:
    parts = request_id.split("_")
    return (
        len(parts) == 2
        and parts[0] == "req"
        and len(parts[1]) in {12, 16}
        and all(ch in "0123456789abcdef" for ch in parts[1])
    )


def _principal_can_access_request_id(principal: AuthPrincipal, request_id: str) -> bool:
    if request_id_belongs_to_client(request_id, principal.client_id):
        return True
    return _legacy_report_ids_allowed() and _is_legacy_public_request_id(request_id)


def _idempotency_key_hash(principal: AuthPrincipal, normalized_key: str) -> str:
    # Pre-tenant idempotency rows contain only sha256(normalized_key), so there is
    # no safe way to assign them among multiple clients. While exactly one client
    # exists, retain that namespace to preserve replay/conflict semantics. The
    # moment multiple distinct clients are configured, switch to client-scoped
    # hashes so tenants cannot collide on the same caller-provided key.
    if _legacy_report_ids_allowed():
        return hash_idempotency_key(normalized_key)
    return scoped_idempotency_key_hash(principal.client_id, normalized_key)


def _assign_or_validate_request_id(
    request: AnalysisRequest,
    principal: AuthPrincipal,
) -> AnalysisRequest:
    if request.request_id is None:
        return request.model_copy(update={"request_id": new_client_request_id(principal.client_id)})
    if _principal_can_access_request_id(principal, request.request_id):
        return request
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_request_id",
            "message": "request_id is not valid for the authenticated client",
        },
    )


async def verify_strict_startup_readiness() -> None:
    """Fail startup without exposing secret values when production dependencies are incomplete."""
    if not settings.strict_startup_readiness:
        return

    missing: list[str] = []
    if not settings.database_url:
        missing.append("database")
    if not settings.openai_api_key:
        missing.append("openai")
    try:
        auth_configured = bool(configured_api_key_hashes(settings))
    except ValueError as exc:
        raise RuntimeError("Strict startup readiness failed: API auth configuration is invalid") from exc
    if not auth_configured:
        missing.append("api_auth")
    if missing:
        raise RuntimeError(
            "Strict startup readiness failed: missing required configuration: " + ",".join(missing)
        )

    try:
        # This harmless indexed lookup establishes DB connectivity and applies the
        # idempotent schema bootstrap through PostgresAuditRepository._ensure_schema.
        await audit_repository.get_report("__startup_readiness_probe__")
    except Exception as exc:
        logger.exception("Strict startup readiness failed: database connectivity unavailable")
        raise RuntimeError("Strict startup readiness failed: database connectivity unavailable") from exc

    logger.info(
        "Strict startup readiness passed: database=true openai_configured=true api_auth_configured=true"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await verify_strict_startup_readiness()
    yield
    close_audit = getattr(audit_repository, "close", None)
    if close_audit is not None:
        await close_audit()
    if edinet_index_repository is not None:
        await edinet_index_repository.close()


docs_url = "/docs" if settings.api_docs_enabled else None
redoc_url = "/redoc" if settings.api_docs_enabled else None
openapi_url = "/openapi.json" if settings.api_docs_enabled else None
app = FastAPI(
    title=settings.api_name,
    version=settings.api_version,
    description=(
        "Point-in-time multi-agent investment research API. Observed evidence, "
        "deterministic calculations, and explicit valuation assumptions are kept separate."
    ),
    lifespan=lifespan,
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Request-ID") or f"http_{uuid.uuid4().hex[:16]}"
    request.state.correlation_id = correlation_id[:128]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.correlation_id
    return response


def _error_payload(
    request: Request,
    *,
    code: str,
    message: str,
    details=None,
) -> dict:
    return ApiErrorResponse(
        error=ApiErrorBody(
            code=code,
            message=message,
            request_id=getattr(request.state, "correlation_id", None),
            details=details,
        )
    ).model_dump(mode="json")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code", "http_error"))
        message = str(detail.get("message", "Request failed"))
        details = detail.get("details")
    else:
        code = "http_error"
        message = str(detail)
        details = None
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(request, code=code, message=message, details=details),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_payload(
            request,
            code="validation_error",
            message="Request validation failed",
            details=jsonable_encoder(exc.errors()),
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_payload(
            request,
            code="internal_error",
            message="The request could not be completed",
        ),
    )


@app.get("/health", response_model=HealthResponse, include_in_schema=False)
@app.get("/v1/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(
        service=settings.api_name,
        version=settings.api_version,
        registered_agents=len(registry.specs),
        auth_enabled=bool(configured_api_key_hashes(settings)),
        audit_persistence=bool(settings.database_url),
        edinet_index=bool(settings.database_url),
    )


def _require_research_runtime() -> None:
    if not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "audit_database_required",
                "message": "DATABASE_URL is required for the public research API",
            },
        )
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "agent_runtime_unavailable",
                "message": "OPENAI_API_KEY is not configured",
            },
        )


@app.post(
    "/v1/research",
    response_model=ResearchResponse,
    tags=["research"],
    responses={401: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse}},
)
async def research(
    request: AnalysisRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_api_key),
) -> ResearchResponse:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required",
            },
        )
    try:
        normalized_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key", "message": str(exc)},
        ) from exc

    request = _assign_or_validate_request_id(request, principal)
    _require_research_runtime()
    request_hash = research_request_hash(request)
    key_hash = _idempotency_key_hash(principal, normalized_key)
    request_id = str(request.request_id)

    reservation = await audit_repository.reserve_idempotency(
        key_hash,
        request_hash,
        request_id,
    )
    if reservation.state == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_reused",
                "message": "Idempotency-Key was already used with a different request payload",
                "details": {"request_id": reservation.request_id},
            },
        )
    if reservation.state == "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "request_in_progress",
                "message": "A request with this Idempotency-Key is still running",
                "details": {"request_id": reservation.request_id},
            },
        )
    if reservation.state == "replay":
        replay_request_id = str(reservation.request_id)
        if not _principal_can_access_request_id(principal, replay_request_id):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "idempotency_state_inconsistent",
                    "message": "Stored idempotency state has invalid ownership",
                },
            )
        replay = await audit_repository.get_report(replay_request_id)
        if replay is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "idempotency_state_inconsistent",
                    "message": "Stored idempotency state references a missing report",
                },
            )
        return ResearchResponse(
            request_id=replay.request_id,
            report=replay,
            idempotent_replay=True,
        )

    audit_saved = False
    try:
        result = await engine.analyze(request)
        await audit_repository.save(
            result.request,
            result.report,
            result.agent_results,
            result.route,
            result.cio_judgement,
        )
        audit_saved = True
        await audit_repository.complete_idempotency(key_hash, request_hash)
        return ResearchResponse(request_id=result.report.request_id, report=result.report)
    except Exception:
        if not audit_saved:
            await audit_repository.release_idempotency(key_hash, request_hash)
        raise


@app.post(
    "/v1/valuation",
    response_model=ValuationResponse,
    tags=["valuation"],
    responses={401: {"model": ApiErrorResponse}},
)
async def valuation(
    payload: ValuationRequest,
    _: AuthPrincipal = Depends(require_api_key),
) -> ValuationResponse:
    request = AnalysisRequest(
        as_of=payload.as_of,
        asset=payload.asset,
        user_question="Deterministic valuation calculation only",
        analysis_types=["valuation"],
        valuation_assumptions=payload.assumptions,
    )
    enriched = await valuation_provider.enrich(request)
    calculations = enriched.fundamental_data.get("valuation_calculations", {})
    return ValuationResponse(
        as_of=payload.as_of,
        asset=payload.asset,
        calculations=calculations,
    )


@app.get(
    "/v1/reports/{request_id}",
    response_model=ResearchResponse,
    tags=["research"],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
)
async def get_report(
    request_id: str,
    principal: AuthPrincipal = Depends(require_api_key),
) -> ResearchResponse:
    if not _principal_can_access_request_id(principal, request_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "report_not_found", "message": "Report was not found"},
        )
    if not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "audit_database_required",
                "message": "DATABASE_URL is required to retrieve reports",
            },
        )
    report = await audit_repository.get_report(request_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "report_not_found", "message": "Report was not found"},
        )
    return ResearchResponse(request_id=report.request_id, report=report, idempotent_replay=True)


@app.post(
    "/v1/route",
    response_model=RouteResponse,
    tags=["research"],
)
async def route(
    request: AnalysisRequest,
    _: AuthPrincipal = Depends(require_api_key),
) -> RouteResponse:
    plan = router.route(request)
    return RouteResponse(
        specialists=plan.specialists,
        verifiers=plan.verifiers,
        critics=plan.critics,
        cio=plan.cio,
        total=len(plan.all_agents),
    )


@app.post(
    "/v1/analyze",
    response_model=InvestmentReport,
    deprecated=True,
    include_in_schema=True,
    tags=["legacy"],
)
async def analyze_legacy(
    request: AnalysisRequest,
    principal: AuthPrincipal = Depends(require_api_key),
) -> InvestmentReport:
    """Backward-compatible endpoint. New integrations should use POST /v1/research."""
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "agent_runtime_unavailable", "message": "OPENAI_API_KEY is not configured"},
        )
    request = _assign_or_validate_request_id(request, principal)
    result = await engine.analyze(request)
    await audit_repository.save(
        result.request,
        result.report,
        result.agent_results,
        result.route,
        result.cio_judgement,
    )
    return result.report
