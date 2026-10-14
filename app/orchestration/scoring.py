from __future__ import annotations

from collections import Counter

from app.domain.schemas import AgentAnalysis, AggregateScore, WeightedComponent


def _source_independence(result: AgentAnalysis, source_counts: Counter[str]) -> float:
    if not result.source_ids:
        return 0.35
    penalties = [1.0 / source_counts[s] for s in set(result.source_ids) if source_counts[s] > 0]
    return max(0.25, min(1.0, sum(penalties) / len(penalties)))


def aggregate_scores(results: list[AgentAnalysis]) -> AggregateScore:
    usable = [r for r in results if r.status == "completed"]
    if not usable:
        return AggregateScore(score=0.0, confidence=0.0, components=[])

    source_counts = Counter(s for r in usable for s in set(r.source_ids))
    components: list[WeightedComponent] = []
    numerator = 0.0
    denominator = 0.0

    for r in usable:
        independence = _source_independence(r, source_counts)
        effective_weight = r.confidence * r.evidence_quality * independence
        components.append(
            WeightedComponent(
                agent_id=r.agent_id,
                score=r.score,
                confidence=r.confidence,
                evidence_quality=r.evidence_quality,
                independence=independence,
                effective_weight=effective_weight,
            )
        )
        numerator += r.score * effective_weight
        denominator += effective_weight

    score = numerator / denominator if denominator else 0.0
    confidence = min(1.0, denominator / max(1.0, len(usable) * 0.65))
    return AggregateScore(score=score, confidence=confidence, components=components)
