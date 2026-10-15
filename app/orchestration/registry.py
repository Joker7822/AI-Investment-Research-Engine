from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.settings import Settings
from app.domain.prompts import SYSTEM_KERNEL
from app.domain.schemas import AgentAnalysis


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    department: str
    mission: str
    tags: tuple[str, ...]
    stage: str
    mandatory: bool = False
    requires_risk_profile: bool = False


class AgentRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        config_path = Path(settings.agent_config_path)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        fields = raw.get("fields")

        def normalize(item):
            if isinstance(item, dict):
                return item
            if not fields:
                raise ValueError("Compact agent rows require a top-level fields list")
            if len(item) != len(fields):
                raise ValueError("Agent row does not match fields schema")
            return dict(zip(fields, item, strict=True))

        normalized = [normalize(item) for item in raw["agents"]]
        self._specs = {
            item["id"]: AgentSpec(
                id=item["id"],
                name=item["name"],
                department=item["department"],
                mission=item["mission"],
                tags=tuple(item.get("tags", [])),
                stage=item.get("stage", "specialist"),
                mandatory=bool(item.get("mandatory", False)),
                requires_risk_profile=bool(item.get("requires_risk_profile", False)),
            )
            for item in normalized
        }
        if len(self._specs) != 100:
            raise ValueError(f"Expected exactly 100 agents; got {len(self._specs)}")

    @property
    def specs(self) -> dict[str, AgentSpec]:
        return self._specs

    def spec(self, agent_id: str) -> AgentSpec:
        return self._specs[agent_id]

    def build(self, agent_id: str, as_of: str, *, model_override: Any | None = None):
        # Lazy import keeps routing/config tests runnable without the runtime SDK installed.
        from agents import Agent

        spec = self.spec(agent_id)
        instructions = SYSTEM_KERNEL.format(
            as_of=as_of,
            agent_id=spec.id,
            department=spec.department,
            mission=spec.mission,
        )
        kwargs = {
            "name": f"{spec.id} {spec.name}",
            "instructions": instructions,
            "output_type": AgentAnalysis,
        }
        if model_override is not None:
            kwargs["model"] = model_override
        elif self.settings.openai_agent_model:
            kwargs["model"] = self.settings.openai_agent_model
        return Agent(**kwargs)
