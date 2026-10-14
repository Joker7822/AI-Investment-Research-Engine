from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from agents import set_default_openai_key

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


settings = get_settings()
if settings.openai_api_key:
    set_default_openai_key(settings.openai_api_key)
registry = AgentRegistry(settings)
router = DeterministicRouter(registry)

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
# Pure-Python deterministic calculations run after primary-source enrichment.
providers.append(DerivedFinancialsProvider())
providers.append(ValuationCalculationProvider())
provider = CompositeResearchDataProvider(providers)
engine = InvestmentAnalysisEngine(settings, registry, router, provider)

audit_repository = (
    PostgresAuditRepository(settings.database_url, settings.database_schema_path)
    if settings.database_url
    else NullAuditRepository()
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_audit = getattr(audit_repository, "close", None)
    if close_audit is not None:
        await close_audit()
    if edinet_index_repository is not None:
        await edinet_index_repository.close()


app = FastAPI(title="Investment AI 100 Agents", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str | int | bool]:
    return {
        "status": "ok",
        "registered_agents": len(registry.specs),
        "audit_persistence": bool(settings.database_url),
        "edinet_index": bool(settings.database_url),
    }


@app.post("/v1/analyze", response_model=InvestmentReport)
async def analyze(request: AnalysisRequest) -> InvestmentReport:
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured. Data routing can be tested without it, but agent execution cannot.",
        )
    result = await engine.analyze(request)
    await audit_repository.save(
        result.request,
        result.report,
        result.agent_results,
        result.route,
        result.cio_judgement,
    )
    return result.report


@app.post("/v1/route")
async def route(request: AnalysisRequest) -> dict[str, object]:
    """No LLM call. Useful for deterministic routing tests and cost estimation."""
    plan = router.route(request)
    return {
        "specialists": plan.specialists,
        "verifiers": plan.verifiers,
        "critics": plan.critics,
        "cio": plan.cio,
        "total": len(plan.all_agents),
    }
