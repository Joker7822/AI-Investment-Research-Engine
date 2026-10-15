from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Security, status
from fastapi.encoders import jsonable_encoder
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from app.api.auth import AuthPrincipal, api_key_header, authenticate_api_key
from app.api.main import (
    engine,
    get_report as internal_get_report,
    research as internal_research,
    router as internal_router,
    settings,
    valuation as internal_valuation,
)
from app.api.models import ValuationRequest
from app.api.public_models import (
    PublicAICustomProbeResponse,
    PublicAIProviderStatusResponse,
    PublicAnalysisStats,
    PublicDCFRequest,
    PublicDCFResponse,
    PublicDecision,
    PublicHealthResponse,
    PublicResearchRequest,
    PublicResearchResponse,
    PublicRouteResponse,
)
from app.domain.schemas import (
    AnalysisRequest,
    AssetRef,
    DCFScenarioAssumption,
    InvestmentReport,
    ValuationAssumptions,
)
from app.orchestration.model_runtime import (
    ModelProviderMode,
    ModelProviderUnavailableError,
    use_model_provider,
)


logger = logging.getLogger(__name__)
public_router = APIRouter(prefix="/api/v1", tags=["public"])


async def require_public_api_key(
    api_key: str | None = Security(api_key_header),
) -> AuthPrincipal:
    """Authenticate a public API key and resolve its stable client identity."""
    return authenticate_api_key(settings, api_key, public_contract=True)


def _asset(payload: PublicResearchRequest | PublicDCFRequest) -> AssetRef:
    return AssetRef(
        type=getattr(payload, "asset_type", "equity"),
        ticker=payload.symbol,
        exchange=payload.market,
        currency=payload.currency,
        region=getattr(payload, "region", None),
    )


def _analysis_request(payload: PublicResearchRequest) -> AnalysisRequest:
    return AnalysisRequest(
        as_of=payload.as_of or datetime.now(timezone.utc),
        asset=_asset(payload),
        user_question=payload.question,
        investment_horizon=payload.horizon,
        analysis_types=list(payload.modules),
    )


def _public_report(report: InvestmentReport) -> PublicResearchResponse:
    return PublicResearchResponse(
        request_id=report.request_id,
        symbol=report.asset.ticker,
        as_of=report.as_of,
        horizon=report.horizon,
        decision=PublicDecision(
            stance=report.stance.value,
            score=round(report.score * 100.0, 4),
            confidence=report.confidence,
            evidence_quality=report.evidence_quality,
        ),
        summary=report.conclusion,
        bull_case=report.bull_case,
        bear_case=report.bear_case,
        risks=report.key_risks,
        invalidators=report.invalidators,
        caveats=report.caveats,
        analysis=PublicAnalysisStats(
            selected_agents=len(report.selected_agents),
            completed_agents=len(report.completed_agents),
            sources=len(report.source_ids),
        ),
        report_url=f"/api/v1/reports/{report.request_id}",
    )


def _public_http_exception(exc: HTTPException) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    internal_code = str(detail.get("code", ""))
    mapping = {
        "audit_database_required": "DATA_UNAVAILABLE",
        "agent_runtime_unavailable": "AI_CAPACITY_UNAVAILABLE",
        "idempotency_state_inconsistent": "DATA_UNAVAILABLE",
        "report_not_found": "REPORT_NOT_FOUND",
        "idempotency_key_reused": "INVALID_REQUEST",
        "request_in_progress": "RATE_LIMITED",
        "invalid_idempotency_key": "INVALID_REQUEST",
        "missing_idempotency_key": "INVALID_REQUEST",
        "invalid_request_id": "INVALID_REQUEST",
        "research_rate_limit_exceeded": "RATE_LIMITED",
        "research_daily_request_quota_exceeded": "RATE_LIMITED",
        "research_daily_agent_quota_exceeded": "RATE_LIMITED",
        "research_request_agent_limit_exceeded": "INVALID_REQUEST",
        "research_usage_limit_exceeded": "RATE_LIMITED",
    }
    public_code = mapping.get(internal_code, "ANALYSIS_FAILED")
    messages = {
        "DATA_UNAVAILABLE": "Required data services are temporarily unavailable",
        "AI_CAPACITY_UNAVAILABLE": "AI analysis is temporarily unavailable",
        "REPORT_NOT_FOUND": "Report was not found",
        "INVALID_REQUEST": "Request could not be accepted",
        "RATE_LIMITED": "Research request is temporarily limited",
        "ANALYSIS_FAILED": "Investment analysis failed",
    }
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "code": public_code,
            "message": messages[public_code],
            "details": detail.get("details") if public_code in {"INVALID_REQUEST", "RATE_LIMITED"} else None,
        },
        headers=exc.headers,
    )


def _ai_unavailable(exc: Exception, *, provider: str | None = None) -> HTTPException:
    logger.warning(
        "Public research AI dependency unavailable: provider=%s reason=%s",
        provider or "unknown",
        type(exc).__name__,
    )
    details = {"provider": provider} if provider in {"openai", "custom"} else None
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "AI_CAPACITY_UNAVAILABLE",
            "message": "AI analysis is temporarily unavailable",
            "details": details,
        },
    )


def _custom_probe_failure(
    code: str,
    message: str,
    *,
    status_code: int = status.HTTP_502_BAD_GATEWAY,
    upstream_status: int | None = None,
) -> HTTPException:
    details = {"upstream_status": upstream_status} if upstream_status is not None else None
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details},
    )


@public_router.get("/health", response_model=PublicHealthResponse)
async def public_health() -> PublicHealthResponse:
    return PublicHealthResponse(service=settings.api_name, version=settings.api_version)


@public_router.get("/ai/status", response_model=PublicAIProviderStatusResponse)
async def public_ai_status(
    _: AuthPrincipal = Depends(require_public_api_key),
) -> PublicAIProviderStatusResponse:
    """Validate the API key and report provider configuration without exposing secrets."""
    custom_configured = engine.model_runtime.fallback_configured
    return PublicAIProviderStatusResponse(
        openai_configured=engine.model_runtime.supports("openai"),
        custom_configured=custom_configured,
        custom_model=str(settings.custom_llm_model) if custom_configured else None,
    )


@public_router.post("/ai/custom/test", response_model=PublicAICustomProbeResponse)
async def public_custom_ai_probe(
    _: AuthPrincipal = Depends(require_public_api_key),
) -> PublicAICustomProbeResponse:
    """Exercise the exact custom structured-output path used by production agents."""
    try:
        model = await engine.model_runtime.probe_custom()
    except ModelProviderUnavailableError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_NOT_CONFIGURED",
            "Custom AI API is not fully configured",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    except AuthenticationError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_AUTH_FAILED",
            "Custom AI API rejected its configured credential",
        ) from exc
    except PermissionDeniedError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_FORBIDDEN",
            "Custom AI API denied access to the configured model",
        ) from exc
    except RateLimitError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_RATE_LIMITED",
            "Custom AI API is rate limited",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    except APITimeoutError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_TIMEOUT",
            "Custom AI API timed out",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        ) from exc
    except APIConnectionError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_UNREACHABLE",
            "Custom AI API could not be reached",
        ) from exc
    except APIStatusError as exc:
        raise _custom_probe_failure(
            "CUSTOM_API_UPSTREAM_ERROR",
            "Custom AI API returned an error response",
            upstream_status=exc.status_code,
        ) from exc
    except Exception as exc:
        logger.exception("Custom AI compatibility probe failed")
        raise _custom_probe_failure(
            "CUSTOM_API_INCOMPATIBLE",
            "Custom AI API is not compatible with the required structured-output contract",
        ) from exc
    return PublicAICustomProbeResponse(model=model)


@public_router.post("/route", response_model=PublicRouteResponse)
async def public_route(
    payload: PublicResearchRequest,
    _: AuthPrincipal = Depends(require_public_api_key),
) -> PublicRouteResponse:
    try:
        plan = internal_router.route(_analysis_request(payload))
    except Exception as exc:
        logger.exception("Public routing failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "ANALYSIS_FAILED", "message": "Analysis routing failed"},
        ) from exc
    return PublicRouteResponse(
        selected=len(plan.all_agents),
        specialists=len(plan.specialists),
        verifiers=len(plan.verifiers),
        critics=len(plan.critics),
        cio=1,
    )


@public_router.post("/research", response_model=PublicResearchResponse)
async def public_research(
    payload: PublicResearchRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ai_provider: ModelProviderMode = Header(default="auto", alias="X-AI-Provider"),
    principal: AuthPrincipal = Depends(require_public_api_key),
) -> PublicResearchResponse:
    """Run research using auto, OpenAI-only, or custom-API-only model selection."""
    stable_key = idempotency_key or f"public-{uuid.uuid4().hex}"
    try:
        with use_model_provider(ai_provider):
            result = await internal_research(_analysis_request(payload), stable_key, principal)
    except ModelProviderUnavailableError as exc:
        raise _ai_unavailable(exc, provider=exc.provider) from exc
    except HTTPException as exc:
        raise _public_http_exception(exc) from exc
    except RateLimitError as exc:
        raise _ai_unavailable(exc, provider=ai_provider) from exc
    except OpenAIError as exc:
        raise _ai_unavailable(exc, provider=ai_provider) from exc
    except Exception as exc:
        logger.exception("Public research failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "ANALYSIS_FAILED", "message": "Investment analysis failed"},
        ) from exc
    return _public_report(result.report)


@public_router.get("/reports/{request_id}", response_model=PublicResearchResponse)
async def public_report(
    request_id: str,
    principal: AuthPrincipal = Depends(require_public_api_key),
) -> PublicResearchResponse:
    try:
        result = await internal_get_report(request_id, principal)
    except HTTPException as exc:
        raise _public_http_exception(exc) from exc
    except Exception as exc:
        logger.exception("Public report retrieval failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DATA_UNAVAILABLE",
                "message": "Required data services are temporarily unavailable",
            },
        ) from exc
    return _public_report(result.report)


@public_router.post("/valuation/dcf", response_model=PublicDCFResponse)
async def public_dcf(
    payload: PublicDCFRequest,
    principal: AuthPrincipal = Depends(require_public_api_key),
) -> PublicDCFResponse:
    as_of = payload.as_of or datetime.now(timezone.utc)
    scenario_kwargs = {
        "name": "base",
        "currency": payload.currency,
        "cash_flow_basis": payload.cash_flow_basis,
        "base_fcf": payload.base_fcf,
        "growth_rates": payload.growth_rates,
        "terminal_growth": payload.terminal_growth,
        "net_debt": payload.net_debt,
        "diluted_shares": payload.diluted_shares,
    }
    if payload.cash_flow_basis == "fcff":
        scenario_kwargs["wacc"] = payload.discount_rate
    else:
        scenario_kwargs["cost_of_equity"] = payload.discount_rate

    try:
        scenario = DCFScenarioAssumption(**scenario_kwargs)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_REQUEST",
                "message": "DCF assumptions are invalid",
                "details": jsonable_encoder(exc.errors(include_url=False)),
            },
        ) from exc

    internal_payload = ValuationRequest(
        as_of=as_of,
        asset=_asset(payload),
        assumptions=ValuationAssumptions(dcf=[scenario], multiples=[]),
    )
    try:
        result = await internal_valuation(internal_payload, principal)
        calculation = result.calculations["dcf"][0]
    except Exception as exc:
        logger.exception("Public DCF calculation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "Valuation calculation failed"},
        ) from exc

    return PublicDCFResponse(
        symbol=payload.symbol,
        as_of=as_of,
        currency=payload.currency,
        cash_flow_basis=payload.cash_flow_basis,
        value_basis=str(calculation["value_basis"]),
        enterprise_value=calculation["enterprise_value"],
        equity_value=calculation["equity_value"],
        implied_value_per_share=calculation["implied_value_per_share"],
        warnings=list(calculation.get("warnings", [])),
    )


__all__ = ["public_router"]
