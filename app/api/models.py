from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.schemas import AssetRef, InvestmentReport, ValuationAssumptions


class ApiErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: Any | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorBody


class ResearchResponse(BaseModel):
    status: Literal["completed"] = "completed"
    request_id: str
    report: InvestmentReport
    idempotent_replay: bool = False


class ValuationRequest(BaseModel):
    as_of: datetime
    asset: AssetRef
    assumptions: ValuationAssumptions

    @model_validator(mode="after")
    def require_scenario(self) -> "ValuationRequest":
        if not self.assumptions.dcf and not self.assumptions.multiples:
            raise ValueError("at least one DCF or multiple scenario is required")
        return self


class ValuationResponse(BaseModel):
    status: Literal["completed"] = "completed"
    as_of: datetime
    asset: AssetRef
    calculations: dict[str, Any]


class RouteResponse(BaseModel):
    specialists: tuple[str, ...]
    verifiers: tuple[str, ...]
    critics: tuple[str, ...]
    cio: str
    total: int = Field(ge=1)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    registered_agents: int
    auth_enabled: bool
    audit_persistence: bool
    edinet_index: bool
