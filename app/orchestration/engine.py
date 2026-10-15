from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from agents import Agent, trace

from app.core.settings import Settings
from app.domain.prompts import CIO_PROMPT
from app.domain.schemas import AnalysisRequest, AgentAnalysis, CIOJudgement, InvestmentReport, Stance
from app.orchestration.model_runtime import ModelProviderMode, ModelRuntime, current_model_provider
from app.orchestration.registry import AgentRegistry
from app.orchestration.router import DeterministicRouter, RoutePlan
from app.orchestration.scoring import aggregate_scores
from app.providers.base import ResearchDataProvider


@dataclass
class EngineResult:
    request: AnalysisRequest
    report: InvestmentReport
    agent_results: list[AgentAnalysis]
    route: RoutePlan
    cio_judgement: CIOJudgement


class InvestmentAnalysisEngine:
    def __init__(
        self,
        settings: Settings,
        registry: AgentRegistry,
        router: DeterministicRouter,
        provider: ResearchDataProvider,
    ):
        self.settings = settings
        self.registry = registry
        self.router = router
        self.provider = provider
        self.model_runtime = ModelRuntime(settings)
        self._semaphore = asyncio.Semaphore(settings.max_parallel_agents)

    async def _run_agent(
        self,
        agent_id: str,
        req: AnalysisRequest,
        prior_results: list[AgentAnalysis] | None = None,
        *,
        model_provider: ModelProviderMode = "auto",
    ) -> AgentAnalysis:
        payload = {
            "request": req.model_dump(mode="json"),
            "prior_results": [r.model_dump(mode="json") for r in (prior_results or [])],
        }

        def build_agent(model_override: Any | None):
            return self.registry.build(
                agent_id,
                req.as_of.isoformat(),
                model_override=model_override,
            )

        async with self._semaphore:
            result = await self.model_runtime.run(
                build_agent,
                json.dumps(payload, ensure_ascii=False),
                provider=model_provider,
            )
        parsed = result.final_output
        if not isinstance(parsed, AgentAnalysis):
            raise TypeError(f"{agent_id} returned unexpected type: {type(parsed)!r}")
        if parsed.agent_id != agent_id:
            parsed = parsed.model_copy(update={"agent_id": agent_id})
        return parsed

    async def _run_parallel(
        self,
        ids: tuple[str, ...],
        req: AnalysisRequest,
        prior: list[AgentAnalysis] | None = None,
        *,
        model_provider: ModelProviderMode = "auto",
    ) -> list[AgentAnalysis]:
        tasks = [
            self._run_agent(agent_id, req, prior, model_provider=model_provider)
            for agent_id in ids
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        outputs: list[AgentAnalysis] = []
        for agent_id, item in zip(ids, settled, strict=True):
            if isinstance(item, Exception):
                outputs.append(
                    AgentAnalysis(
                        agent_id=agent_id,
                        status="failed",
                        stance=Stance.neutral,
                        score=0.0,
                        confidence=0.0,
                        evidence_quality=0.0,
                        missing_data=[f"Agent execution failed: {item.__class__.__name__}"],
                    )
                )
            else:
                outputs.append(item)
        return outputs

    def _build_cio_agent(self, model_override: Any | None):
        kwargs = {
            "name": "A100 Chief Investment Officer",
            "instructions": CIO_PROMPT,
            "output_type": CIOJudgement,
        }
        if model_override is not None:
            kwargs["model"] = model_override
        elif self.settings.openai_agent_model:
            kwargs["model"] = self.settings.openai_agent_model
        return Agent(**kwargs)

    async def analyze(
        self,
        req: AnalysisRequest,
        *,
        model_provider: ModelProviderMode = "auto",
    ) -> EngineResult:
        req = await self.provider.enrich(req)
        request_id = req.request_id or f"req_{uuid.uuid4().hex[:12]}"
        req = req.model_copy(update={"request_id": request_id})
        route = self.router.route(req)
        effective_provider = current_model_provider(model_provider)

        # A custom-only run should not create OpenAI traces. Auto mode retains
        # tracing for the primary OpenAI path; custom fallback Runner calls disable
        # tracing individually inside ModelRuntime.
        with trace(
            "investment_analysis",
            group_id=request_id,
            metadata={"ticker": req.asset.ticker, "as_of": req.as_of.isoformat()},
            disabled=effective_provider == "custom",
        ):
            specialists = await self._run_parallel(
                route.specialists,
                req,
                model_provider=effective_provider,
            )
            verifiers = await self._run_parallel(
                route.verifiers,
                req,
                specialists,
                model_provider=effective_provider,
            )
            verified_context = specialists + verifiers
            critics = await self._run_parallel(
                route.critics,
                req,
                verified_context,
                model_provider=effective_provider,
            )
            pre_cio = verified_context + critics

            aggregate = aggregate_scores(specialists)

            cio_input = {
                "request": req.model_dump(mode="json"),
                "selected_agents": list(route.all_agents),
                "aggregate": aggregate.model_dump(mode="json"),
                "agent_results": [r.model_dump(mode="json") for r in pre_cio],
                "hard_rules": {
                    "completed_agents_must_be_subset_of_selected": True,
                    "do_not_invent_sources": True,
                    "do_not_invent_price_targets": True,
                },
            }
            cio_result = await self.model_runtime.run(
                self._build_cio_agent,
                json.dumps(cio_input, ensure_ascii=False),
                provider=effective_provider,
            )
            judgement = cio_result.final_output
            if not isinstance(judgement, CIOJudgement):
                raise TypeError(f"CIO returned unexpected type: {type(judgement)!r}")

            evidence_judge = next(
                (r for r in verifiers if r.agent_id == "A90" and r.status == "completed"),
                None,
            )
            if evidence_judge is not None:
                evidence_quality = evidence_judge.evidence_quality
            else:
                completed = [r.evidence_quality for r in verifiers if r.status == "completed"]
                evidence_quality = sum(completed) / len(completed) if completed else 0.0

            final_confidence = min(aggregate.confidence, evidence_quality)
            score = aggregate.score
            if score <= -0.6:
                stance = Stance.very_bearish
            elif score <= -0.2:
                stance = Stance.bearish
            elif score < 0.2:
                stance = Stance.neutral
            elif score < 0.6:
                stance = Stance.bullish
            else:
                stance = Stance.very_bullish

            report = InvestmentReport(
                request_id=request_id,
                asset=req.asset,
                as_of=req.as_of,
                horizon=req.investment_horizon,
                conclusion=judgement.conclusion,
                stance=stance,
                score=score,
                confidence=final_confidence,
                evidence_quality=evidence_quality,
                bull_case=judgement.bull_case,
                bear_case=judgement.bear_case,
                key_risks=judgement.key_risks,
                invalidators=judgement.invalidators,
                selected_agents=list(route.all_agents),
                completed_agents=[r.agent_id for r in pre_cio if r.status == "completed"] + ["A100"],
                source_ids=sorted({source_id for r in pre_cio for source_id in r.source_ids}),
                caveats=judgement.caveats,
            )

        return EngineResult(
            request=req,
            report=report,
            agent_results=pre_cio,
            route=route,
            cio_judgement=judgement,
        )
