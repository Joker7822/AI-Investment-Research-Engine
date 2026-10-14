from fastapi import FastAPI, HTTPException
from agents import set_default_openai_key

from app.core.settings import get_settings
from app.domain.schemas import AnalysisRequest, InvestmentReport
from app.orchestration.engine import InvestmentAnalysisEngine
from app.orchestration.registry import AgentRegistry
from app.orchestration.router import DeterministicRouter
from app.providers.base import CompositeResearchDataProvider, PassthroughDataProvider
from app.providers.massive import MassiveMarketDataProvider
from app.providers.sec import SecCompanyFactsProvider


settings = get_settings()
if settings.openai_api_key:
    set_default_openai_key(settings.openai_api_key)
registry = AgentRegistry(settings)
router = DeterministicRouter(registry)

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
provider = CompositeResearchDataProvider(providers) if providers else PassthroughDataProvider()
engine = InvestmentAnalysisEngine(settings, registry, router, provider)

app = FastAPI(title="Investment AI 100 Agents", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str | int]:
    return {"status": "ok", "registered_agents": len(registry.specs)}


@app.post("/v1/analyze", response_model=InvestmentReport)
async def analyze(request: AnalysisRequest) -> InvestmentReport:
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured. Data routing can be tested without it, but agent execution cannot.",
        )
    result = await engine.analyze(request)
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
