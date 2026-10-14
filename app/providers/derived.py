from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable

from app.domain.schemas import AnalysisRequest
from app.providers.base import ResearchDataProvider


@dataclass(frozen=True)
class InputValue:
    metric_name: str
    period_key: str
    value: Decimal
    unit_id: str | None
    scope: str | None
    accounting_standard: str | None
    doc_id: str | None
    provenance: dict[str, Any]


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _input(metrics: dict[str, Any], metric_name: str, period_key: str) -> InputValue | None:
    raw = metrics.get(metric_name, {}).get(period_key)
    if not isinstance(raw, dict):
        return None
    value = _decimal(raw.get("numeric_value"))
    if value is None:
        return None
    return InputValue(
        metric_name=metric_name,
        period_key=period_key,
        value=value,
        unit_id=raw.get("unit_id"),
        scope=raw.get("scope"),
        accounting_standard=raw.get("accounting_standard"),
        doc_id=raw.get("doc_id"),
        provenance={
            key: raw.get(key)
            for key in (
                "doc_id", "element_id", "context_id", "source_file", "row_number",
                "mapping_version", "raw_value", "numeric_value", "unit_id",
                "unit_label", "scope", "accounting_standard",
            )
        },
    )


def _compatible(*values: InputValue, require_same_doc: bool = True) -> tuple[bool, str | None]:
    if not values:
        return False, "no_inputs"
    standards = {value.accounting_standard for value in values if value.accounting_standard}
    if len(standards) > 1:
        return False, "accounting_standard_mismatch"
    scopes = {value.scope for value in values if value.scope}
    if len(scopes) > 1:
        return False, "scope_mismatch"
    units = {value.unit_id for value in values if value.unit_id}
    if len(units) > 1:
        return False, "unit_mismatch"
    if require_same_doc:
        docs = {value.doc_id for value in values if value.doc_id}
        if len(docs) > 1:
            return False, "source_filing_mismatch"
    return True, None


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 34
        return numerator / denominator


def _growth(current: Decimal, prior: Decimal) -> Decimal | None:
    # (current - prior) / prior avoids a second arithmetic operation under the
    # process-global Decimal context after the 34-digit ratio has been produced.
    return _ratio(current - prior, prior)


def _average(a: Decimal, b: Decimal) -> Decimal:
    return (a + b) / Decimal(2)


def _serialize(value: Decimal) -> str:
    return format(value, "f")


def _calculation(
    *, formula: str, value: Decimal, inputs: list[InputValue], notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "value": _serialize(value),
        "unit": "ratio",
        "formula": formula,
        "inputs": [item.provenance | {"metric_name": item.metric_name, "period_key": item.period_key} for item in inputs],
        "notes": notes or [],
    }


def derive_edinet_metrics(metrics: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Compute conservative ratios only from compatible annual EDINET Silver inputs.

    We intentionally refuse to mix filings, accounting standards, scopes, or units.
    Skipped calculations are returned as machine-readable diagnostics instead of
    silently substituting inputs from another period/source.
    """
    derived: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []

    def get(name: str, period: str = "CurrentYear") -> InputValue | None:
        return _input(metrics, name, period)

    def record_skip(name: str, reason: str) -> None:
        skipped.append({"metric": name, "reason": reason})

    def two_input(
        name: str,
        left: InputValue | None,
        right: InputValue | None,
        formula: str,
        fn: Callable[[Decimal, Decimal], Decimal | None],
    ) -> None:
        if left is None or right is None:
            record_skip(name, "missing_input")
            return
        ok, reason = _compatible(left, right)
        if not ok:
            record_skip(name, reason or "incompatible_inputs")
            return
        value = fn(left.value, right.value)
        if value is None:
            record_skip(name, "zero_denominator")
            return
        derived[name] = _calculation(formula=formula, value=value, inputs=[left, right])

    current_revenue = get("revenue")
    prior_revenue = get("revenue", "Prior1Year")
    current_operating_income = get("operating_income")
    current_net_income = get("net_income")
    current_cfo = get("cf_operating")
    current_assets = get("total_assets")
    prior_assets = get("total_assets", "Prior1Year")
    current_equity = get("net_assets")
    prior_equity = get("net_assets", "Prior1Year")

    two_input("revenue_growth_yoy", current_revenue, prior_revenue,
              "revenue(CurrentYear) / revenue(Prior1Year) - 1", _growth)
    two_input("operating_margin", current_operating_income, current_revenue,
              "operating_income(CurrentYear) / revenue(CurrentYear)", _ratio)
    two_input("net_margin", current_net_income, current_revenue,
              "net_income(CurrentYear) / revenue(CurrentYear)", _ratio)
    two_input("operating_cash_flow_margin", current_cfo, current_revenue,
              "cf_operating(CurrentYear) / revenue(CurrentYear)", _ratio)
    two_input("asset_growth_yoy", current_assets, prior_assets,
              "total_assets(CurrentYear) / total_assets(Prior1Year) - 1", _growth)
    two_input("equity_growth_yoy", current_equity, prior_equity,
              "net_assets(CurrentYear) / net_assets(Prior1Year) - 1", _growth)
    two_input("equity_ratio_calculated", current_equity, current_assets,
              "net_assets(CurrentYear) / total_assets(CurrentYear)", _ratio)
    two_input("cash_conversion", current_cfo, current_net_income,
              "cf_operating(CurrentYear) / net_income(CurrentYear)", _ratio)

    def average_balance_ratio(
        name: str,
        income: InputValue | None,
        current_balance: InputValue | None,
        prior_balance: InputValue | None,
        denominator_name: str,
    ) -> None:
        if income is None or current_balance is None or prior_balance is None:
            record_skip(name, "missing_input")
            return
        ok, reason = _compatible(income, current_balance, prior_balance)
        if not ok:
            record_skip(name, reason or "incompatible_inputs")
            return
        average_balance = _average(current_balance.value, prior_balance.value)
        value = _ratio(income.value, average_balance)
        if value is None:
            record_skip(name, "zero_average_balance")
            return
        notes = []
        if current_balance.value <= 0 or prior_balance.value <= 0:
            notes.append("non_positive_balance_makes_ratio_economically_unstable")
        derived[name] = _calculation(
            formula=f"net_income(CurrentYear) / average({denominator_name}(CurrentYear), {denominator_name}(Prior1Year))",
            value=value,
            inputs=[income, current_balance, prior_balance],
            notes=notes,
        )

    average_balance_ratio("roa_calculated", current_net_income, current_assets, prior_assets, "total_assets")
    average_balance_ratio("roe_calculated", current_net_income, current_equity, prior_equity, "net_assets")
    return derived, skipped


class DerivedFinancialsProvider(ResearchDataProvider):
    """Append deterministic derived metrics after primary-source enrichment."""

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        fundamental = dict(request.fundamental_data)
        edinet = fundamental.get("edinet")
        if not isinstance(edinet, dict):
            return request
        metrics = edinet.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            return request

        derived, skipped = derive_edinet_metrics(metrics)
        edinet_copy = dict(edinet)
        edinet_copy["derived"] = {
            "calculation_policy": "same_doc_same_standard_same_scope_same_unit",
            "metrics": derived,
            "skipped": skipped,
        }
        fundamental["edinet"] = edinet_copy
        return request.model_copy(update={"fundamental_data": fundamental})
