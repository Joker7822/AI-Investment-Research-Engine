from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Stance(str, Enum):
    very_bearish = "very_bearish"
    bearish = "bearish"
    neutral = "neutral"
    bullish = "bullish"
    very_bullish = "very_bullish"


class AssetRef(BaseModel):
    type: Literal["equity", "etf", "index", "bond", "fx", "commodity", "crypto", "other"] = "equity"
    ticker: str
    exchange: str | None = None
    currency: str | None = None
    name: str | None = None
    region: str | None = None
    sector: str | None = None


class EvidenceRef(BaseModel):
    source_id: str
    claim: str
    source_type: str = "unknown"
    observed_at: datetime | None = None
    url: str | None = None
    quality: float = Field(default=0.5, ge=0.0, le=1.0)


class DCFScenarioAssumption(BaseModel):
    name: str = "base"
    currency: str
    base_fcf: Decimal
    growth_rates: list[Decimal] = Field(min_length=1, max_length=20)
    wacc: Decimal
    terminal_growth: Decimal
    net_debt: Decimal = Decimal("0")
    diluted_shares: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_rates(self) -> "DCFScenarioAssumption":
        if self.wacc <= self.terminal_growth:
            raise ValueError("DCF requires wacc > terminal_growth")
        if self.wacc <= Decimal("-1"):
            raise ValueError("wacc must be greater than -1")
        if self.terminal_growth <= Decimal("-1"):
            raise ValueError("terminal_growth must be greater than -1")
        if any(rate <= Decimal("-1") for rate in self.growth_rates):
            raise ValueError("each forecast growth rate must be greater than -1")
        return self


class MultipleScenarioAssumption(BaseModel):
    name: str
    currency: str
    metric_name: str
    metric_value: Decimal = Field(gt=0)
    multiple: Decimal = Field(gt=0)
    basis: Literal["equity", "enterprise"]
    net_debt: Decimal = Decimal("0")
    diluted_shares: Decimal = Field(gt=0)


class ValuationAssumptions(BaseModel):
    dcf: list[DCFScenarioAssumption] = Field(default_factory=list, max_length=10)
    multiples: list[MultipleScenarioAssumption] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_scenario_names(self) -> "ValuationAssumptions":
        dcf_names = [scenario.name for scenario in self.dcf]
        multiple_names = [scenario.name for scenario in self.multiples]
        if len(dcf_names) != len(set(dcf_names)):
            raise ValueError("DCF scenario names must be unique")
        if len(multiple_names) != len(set(multiple_names)):
            raise ValueError("multiple scenario names must be unique")
        return self


class AnalysisRequest(BaseModel):
    request_id: str | None = None
    as_of: datetime
    asset: AssetRef
    user_question: str
    investment_horizon: str = "12m"
    analysis_types: list[str] = Field(default_factory=lambda: ["fundamental", "valuation", "risk"])
    portfolio_specific: bool = False
    risk_profile_available: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)
    market_data: dict[str, Any] = Field(default_factory=dict)
    fundamental_data: dict[str, Any] = Field(default_factory=dict)
    filings: list[dict[str, Any]] = Field(default_factory=list)
    news: list[dict[str, Any]] = Field(default_factory=list)
    portfolio_context: dict[str, Any] | None = None
    valuation_assumptions: ValuationAssumptions | None = None


class Fact(BaseModel):
    claim: str
    value: str | float | int | None = None
    unit: str | None = None
    period: str | None = None
    source_id: str | None = None


class ReasonedClaim(BaseModel):
    claim: str
    logic: str
    impact: float = Field(ge=-1.0, le=1.0)


class AgentAnalysis(BaseModel):
    agent_id: str
    status: Literal["completed", "insufficient_data", "failed"] = "completed"
    stance: Stance = Stance.neutral
    score: float = Field(default=0.0, ge=-1.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    facts: list[Fact] = Field(default_factory=list)
    analysis: list[ReasonedClaim] = Field(default_factory=list)
    bull_factors: list[str] = Field(default_factory=list)
    bear_factors: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class WeightedComponent(BaseModel):
    agent_id: str
    score: float
    confidence: float
    evidence_quality: float
    independence: float
    effective_weight: float


class AggregateScore(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    components: list[WeightedComponent]


class CIOJudgement(BaseModel):
    conclusion: str
    bull_case: list[str] = Field(default_factory=list)
    bear_case: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class InvestmentReport(BaseModel):
    request_id: str
    asset: AssetRef
    as_of: datetime
    horizon: str
    conclusion: str
    stance: Stance
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quality: float = Field(ge=0.0, le=1.0)
    bull_case: list[str]
    bear_case: list[str]
    key_risks: list[str]
    invalidators: list[str]
    selected_agents: list[str]
    completed_agents: list[str]
    source_ids: list[str]
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def clamp_consistency(self) -> "InvestmentReport":
        if not set(self.completed_agents).issubset(set(self.selected_agents)):
            raise ValueError("completed_agents must be a subset of selected_agents")
        return self
