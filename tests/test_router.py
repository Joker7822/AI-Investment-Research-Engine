from datetime import datetime, timezone

from app.core.settings import Settings
from app.domain.schemas import AnalysisRequest, AssetRef
from app.orchestration.registry import AgentRegistry
from app.orchestration.router import DeterministicRouter


def make_request(**overrides):
    data = dict(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="TEST", region="US", sector="technology"),
        user_question="Is this equity attractive over 12 months?",
        investment_horizon="12m",
        analysis_types=["fundamental", "valuation", "risk", "scenario"],
    )
    data.update(overrides)
    return AnalysisRequest(**data)


def test_registry_has_exactly_100_agents():
    registry = AgentRegistry(Settings(agent_config_path="config/agents.yaml"))
    assert len(registry.specs) == 100
    assert set(registry.specs) == ({f"A{i:02d}" for i in range(1, 100)} | {"A100"})


def test_route_includes_mandatory_control_agents():
    registry = AgentRegistry(Settings(agent_config_path="config/agents.yaml"))
    router = DeterministicRouter(registry)
    plan = router.route(make_request())
    selected = set(plan.all_agents)
    assert {"A81", "A82", "A89", "A90", "A98", "A99", "A100"}.issubset(selected)


def test_position_sizing_is_blocked_without_risk_profile():
    registry = AgentRegistry(Settings(agent_config_path="config/agents.yaml"))
    router = DeterministicRouter(registry, max_specialists=80)
    req = make_request(analysis_types=["portfolio"], risk_profile_available=False)
    plan = router.route(req)
    assert "A62" not in plan.specialists


def test_position_sizing_can_run_with_risk_profile():
    registry = AgentRegistry(Settings(agent_config_path="config/agents.yaml"))
    router = DeterministicRouter(registry, max_specialists=80)
    req = make_request(analysis_types=["portfolio"], risk_profile_available=True)
    plan = router.route(req)
    assert "A62" in plan.specialists
