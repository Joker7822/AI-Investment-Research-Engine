from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.schemas import AnalysisRequest


class ResearchDataProvider(ABC):
    @abstractmethod
    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        """Return a request enriched with normalized, timestamped source data."""
        raise NotImplementedError


class PassthroughDataProvider(ResearchDataProvider):
    """MVP provider: uses only data already supplied by the API caller."""

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        return request


class CompositeResearchDataProvider(ResearchDataProvider):
    def __init__(self, providers: list[ResearchDataProvider]):
        self.providers = providers

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        enriched = request
        for provider in self.providers:
            enriched = await provider.enrich(enriched)
        return enriched
