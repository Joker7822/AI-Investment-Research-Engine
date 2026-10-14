import json
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.schemas import (
    AnalysisRequest,
    AssetRef,
    CIOJudgement,
    DCFScenarioAssumption,
    InvestmentReport,
    Stance,
    ValuationAssumptions,
)
from app.orchestration.router import RoutePlan
from app.persistence.audit import NullAuditRepository, _json, stage_for_agent


def _route() -> RoutePlan:
    return RoutePlan(
        specialists=("A11", "A21"),
        verifiers=("A81", "A90"),
        critics=("A91", "A99"),
        cio="A100",
    )


def test_stage_for_agent_is_deterministic():
    route = _route()
    assert stage_for_agent("A11", route) == "specialist"
    assert stage_for_agent("A90", route) == "verification"
    assert stage_for_agent("A99", route) == "critic"
    assert stage_for_agent("A100", route) == "cio"
    assert stage_for_agent("A55", route) == "unknown"


def test_audit_json_preserves_decimal_valuation_assumptions():
    request = AnalysisRequest(
        request_id="req_valuation",
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="AAPL", currency="USD"),
        user_question="valuation",
        valuation_assumptions=ValuationAssumptions(
            dcf=[
                DCFScenarioAssumption(
                    name="base",
                    currency="USD",
                    base_fcf=Decimal("100.25"),
                    growth_rates=[Decimal("0.05")],
                    wacc=Decimal("0.10"),
                    terminal_growth=Decimal("0.02"),
                    diluted_shares=Decimal("10"),
                )
            ]
        ),
    )

    payload = json.loads(_json(request))
    scenario = payload["valuation_assumptions"]["dcf"][0]
    assert scenario["base_fcf"] == "100.25"
    assert scenario["wacc"] == "0.10"
    assert scenario["growth_rates"] == ["0.05"]


async def test_null_audit_repository_is_noop():
    request = AnalysisRequest(
        request_id="req_test",
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="AAPL", exchange="NASDAQ", region="US"),
        user_question="Analyze Apple",
    )
    report = InvestmentReport(
        request_id="req_test",
        asset=request.asset,
        as_of=request.as_of,
        horizon="12m",
        conclusion="neutral test",
        stance=Stance.neutral,
        score=0.0,
        confidence=0.5,
        evidence_quality=0.5,
        bull_case=[],
        bear_case=[],
        key_risks=[],
        invalidators=[],
        selected_agents=list(_route().all_agents),
        completed_agents=["A100"],
        source_ids=[],
    )
    judgement = CIOJudgement(conclusion="neutral test")

    repo = NullAuditRepository()
    assert await repo.save(request, report, [], _route(), judgement) is None
