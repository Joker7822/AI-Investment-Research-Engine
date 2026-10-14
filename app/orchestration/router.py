from __future__ import annotations

from dataclasses import dataclass

from app.domain.schemas import AnalysisRequest
from app.orchestration.registry import AgentRegistry


MVP_CORE = {
    "A01", "A05", "A11", "A12", "A13", "A16", "A21", "A22", "A31", "A42",
    "A51", "A58", "A71", "A72", "A73", "A81", "A82", "A83", "A89", "A90",
    "A91", "A92", "A94", "A98", "A99", "A100",
}

TYPE_TAGS = {
    "fundamental": {"fundamental"},
    "valuation": {"valuation"},
    "market": {"market", "technical"},
    "technical": {"market", "technical"},
    "sentiment": {"information", "sentiment"},
    "risk": {"risk"},
    "portfolio": {"portfolio"},
    "scenario": {"scenario"},
    "macro": {"macro"},
}

MANDATORY_FINAL = {"A81", "A82", "A89", "A90", "A98", "A99", "A100"}


@dataclass(frozen=True)
class RoutePlan:
    specialists: tuple[str, ...]
    verifiers: tuple[str, ...]
    critics: tuple[str, ...]
    cio: str

    @property
    def all_agents(self) -> tuple[str, ...]:
        return self.specialists + self.verifiers + self.critics + (self.cio,)


class DeterministicRouter:
    """Rules first. LLM routing can be layered later, but cannot remove mandatory checks."""

    def __init__(self, registry: AgentRegistry, max_specialists: int = 18):
        self.registry = registry
        self.max_specialists = max_specialists

    def route(self, req: AnalysisRequest) -> RoutePlan:
        desired_tags: set[str] = set()
        for analysis_type in req.analysis_types:
            desired_tags |= TYPE_TAGS.get(analysis_type.lower(), {analysis_type.lower()})

        ranked: list[tuple[int, str]] = []
        for agent_id, spec in self.registry.specs.items():
            if spec.stage != "specialist":
                continue
            if spec.requires_risk_profile and not req.risk_profile_available:
                continue
            score = len(desired_tags.intersection(set(spec.tags)))
            if req.asset.region and req.asset.region.lower() in {t.lower() for t in spec.tags}:
                score += 2
            if req.asset.sector and req.asset.sector.lower() in {t.lower() for t in spec.tags}:
                score += 1
            if agent_id in MVP_CORE:
                score += 1
            if score > 0:
                ranked.append((score, agent_id))

        ranked.sort(key=lambda x: (-x[0], x[1]))
        specialists = [agent_id for _, agent_id in ranked[: self.max_specialists]]

        if req.asset.type == "equity":
            for agent_id in ("A11", "A13", "A21", "A51"):
                if agent_id not in specialists:
                    specialists.append(agent_id)

        verifiers = [
            aid for aid in ("A81", "A82", "A83", "A89", "A90")
            if aid in self.registry.specs
        ]
        critics = [
            aid for aid in ("A91", "A92", "A94", "A98", "A99")
            if aid in self.registry.specs
        ]

        selected = set(specialists) | set(verifiers) | set(critics) | {"A100"}
        assert MANDATORY_FINAL.issubset(selected)

        return RoutePlan(
            specialists=tuple(specialists),
            verifiers=tuple(verifiers),
            critics=tuple(critics),
            cio="A100",
        )
