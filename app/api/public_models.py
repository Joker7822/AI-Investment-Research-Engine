from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


PublicModule = Literal[
    "fundamental",
    "valuation",
    "risk",
    "market",
    "macro",
    "scenario",
]
PublicAssetType = Literal["equity", "etf", "index", "bond", "fx", "commodity", "crypto", "other"]


class PublicResearchRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, max_length=8)
    region: str | None = Field(default=None, max_length=32)
    asset_type: PublicAssetType = "equity"
    question: str = Field(min_length=1, max_length=4000)
    horizon: str = Field(default="12m", min_length=1, max_length=32)
    modules: list[PublicModule] = Field(
        default_factory=lambda: ["fundamental", "valuation", "risk"],
        min_length=1,
        max_length=6,
    )
    as_of: datetime | None = None

    @model_validator(mode="after")
    def normalize(self) -> "PublicResearchRequest":
        self.symbol = self.symbol.strip().upper()
        if self.market is not None:
            self.market = self.market.strip().upper() or None
        if self.currency is not None:
            self.currency = self.currency.strip().upper() or None
        if self.region is not None:
            self.region = self.region.strip().upper() or None
        self.question = self.question.strip()
        self.horizon = self.horizon.strip()
        self.modules = list(dict.fromkeys(self.modules))
        return self


class PublicDecision(BaseModel):
    stance: str
    score: float = Field(ge=-100.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quality: float = Field(ge=0.0, le=1.0)


class PublicAnalysisStats(BaseModel):
    selected_agents: int = Field(ge=1)
    completed_agents: int = Field(ge=0)
    sources: int = Field(ge=0)


class PublicResearchResponse(BaseModel):
    status: Literal["completed"] = "completed"
    request_id: str
    symbol: str
    as_of: datetime
    horizon: str
    decision: PublicDecision
    summary: str
    bull_case: list[str]
    bear_case: list[str]
    risks: list[str]
    invalidators: list[str]
    caveats: list[str]
    analysis: PublicAnalysisStats
    report_url: str


class PublicRouteResponse(BaseModel):
    status: Literal["ok"] = "ok"
    selected: int = Field(ge=1)
    specialists: int = Field(ge=0)
    verifiers: int = Field(ge=0)
    critics: int = Field(ge=0)
    cio: int = Field(default=1, ge=1, le=1)


class PublicHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str


class PublicDCFRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: str | None = Field(default=None, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    as_of: datetime | None = None
    cash_flow_basis: Literal["fcff", "fcfe"] = "fcff"
    base_fcf: Decimal
    growth_rates: list[Decimal] = Field(min_length=1, max_length=20)
    discount_rate: Decimal
    terminal_growth: Decimal
    net_debt: Decimal = Decimal("0")
    diluted_shares: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def normalize(self) -> "PublicDCFRequest":
        self.symbol = self.symbol.strip().upper()
        if self.market is not None:
            self.market = self.market.strip().upper() or None
        self.currency = self.currency.strip().upper()
        return self


class PublicDCFResponse(BaseModel):
    status: Literal["completed"] = "completed"
    symbol: str
    as_of: datetime
    currency: str
    cash_flow_basis: Literal["fcff", "fcfe"]
    value_basis: str
    enterprise_value: Decimal
    equity_value: Decimal
    implied_value_per_share: Decimal
    warnings: list[str]
