from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Security, status
from openai import OpenAIError, RateLimitError
from pydantic import ValidationError

from app.api.auth import api_key_header, configured_api_key_hashes, hash_api_key
from app.api.main import (
    get_report as internal_get_report,
    research as internal_research,
    router as internal_router,
    settings,
    valuation as internal_valuation,
)
from app.api.models import ValuationRequest
from app.api.public_models import (
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


logger = logging.getLogger(__name__)
public_router = APIRouter(prefix="/api/v1", tags=["public"])
_PUBLIC_API_KEY_HASHES = configured_api_key_hashes(settings)


async def require_public_api_key(api_key: str | None = Security(api_key_header)) -> str:
    """Stable public authentication contract, independent of internal error names."""
    if not _PUBLIC_API_KEY_HASHES:
        return "auth-disabled"
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "X-API-Key is required"},
        )
    candidate = hash_api_key(api_key)
    if not any(hmac.compare_digest(candidate, allowed) for allowed in _PUBLIC_API_KEY_HASHES):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "API key is invalid"},
        )
    return candidate


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
    }
    public_code = mapping.get(internal_code, "ANALYSIS_FAILED")
    messages = {
        "DATA_UNAVAILABLE": "Required data services are temporarily unavailable",
        "AI_CAPACITY_UNAVAILABLE": "AI analysis is temporarily unavailable",
        "REPORT_NOT_FOUND": "Report was not found",
        "INVALID_REQUEST": "Request could not be accepted",
        "RATE_LIMITED": "A matching request is already in progress",
        "ANALYSIS_FAILED": "Investment analysis failed",
    }
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "code": public_code,
            "message": messages[public_code],
            "details": detail.get("details") if public_code in {"INVALID_REQUEST", "RATE_LIMITED"} else None,
        },
    )


def _ai_unavailable(exc: Exception) -> HTTPException:
    logger.warning("Public research AI dependency unavailable: %s", type(exc).__name__)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "AI_CAPACITY_UNAVAILABLE",
            "message": "AI analysis is temporarily unavailable",
        },
    )


@public_router.get("/health", response_model=PublicHealthResponse)
async def public_health() -> PublicHealthResponse:
    return PublicHealthResponse(service=settings.api_name, version=settings.api_version)


@public_router.post("/route", response_model=PublicRouteResponse)
async def public_route(
    payload: PublicResearchRequest,
    _: str = Depends(require_public_api_key),
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
    principal: str = Depends(require_public_api_key),
) -> PublicResearchResponse:
    stable_key = idempotency_key or f"public-{uuid.uuid4().hex}"
    try:
        result = await internal_research(_analysis_request(payload), stable_key, principal)
    except HTTPException as exc:
        raise _public_http_exception(exc) from exc
    except RateLimitError as exc:
        raise _ai_unavailable(exc) from exc
    except OpenAIError as exc:
        raise _ai_unavailable(exc) from exc
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
    principal: str = Depends(require_public_api_key),
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
    principal: str = Depends(require_public_api_key),
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
                "details": exc.errors(include_url=False),
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
