from app.domain.schemas import AgentAnalysis, Stance
from app.orchestration.scoring import aggregate_scores


def result(agent_id, score, source_ids, confidence=1.0, evidence_quality=1.0):
    return AgentAnalysis(
        agent_id=agent_id,
        stance=Stance.bullish if score > 0 else Stance.bearish,
        score=score,
        confidence=confidence,
        evidence_quality=evidence_quality,
        source_ids=source_ids,
    )


def test_duplicate_source_reduces_independence():
    agg = aggregate_scores([
        result("A11", 1.0, ["same"]),
        result("A12", 1.0, ["same"]),
        result("A13", -1.0, ["independent"]),
    ])
    weights = {c.agent_id: c.effective_weight for c in agg.components}
    assert weights["A11"] < weights["A13"]
    assert weights["A12"] < weights["A13"]


def test_no_usable_results_is_neutral():
    agg = aggregate_scores([
        AgentAnalysis(agent_id="A11", status="failed", score=0, confidence=0, evidence_quality=0)
    ])
    assert agg.score == 0
    assert agg.confidence == 0
