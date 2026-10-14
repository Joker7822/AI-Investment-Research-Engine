from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from app.domain.schemas import (
    AnalysisRequest,
    DCFScenarioAssumption,
    MultipleScenarioAssumption,
)
from app.providers.base import ResearchDataProvider


def _s(value: Decimal) -> str:
    return format(value, "f")


def _pow(base: Decimal, exponent: int) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        return base ** exponent


def _div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise ValueError("division by zero")
    with localcontext() as context:
        context.prec = 34
        return numerator / denominator


def _dcf_discount_rate(scenario: DCFScenarioAssumption) -> tuple[str, Decimal]:
    if scenario.cash_flow_basis == "fcff":
        if scenario.wacc is None:
            raise ValueError("FCFF DCF requires wacc")
        return "wacc", scenario.wacc
    if scenario.cost_of_equity is None:
        raise ValueError("FCFE DCF requires cost_of_equity")
    return "cost_of_equity", scenario.cost_of_equity


def calculate_dcf(scenario: DCFScenarioAssumption) -> dict[str, Any]:
    """Run a deterministic Gordon-growth DCF from explicit FCFF or FCFE assumptions."""
    discount_rate_name, discount_rate = _dcf_discount_rate(scenario)
    one = Decimal("1")
    discount_base = one + discount_rate
    forecast_fcf = scenario.base_fcf
    forecast: list[dict[str, str | int]] = []
    pv_forecast = Decimal("0")

    with localcontext() as context:
        context.prec = 34
        for year, growth in enumerate(scenario.growth_rates, start=1):
            forecast_fcf = forecast_fcf * (one + growth)
            discount_factor = _pow(discount_base, year)
            present_value = _div(forecast_fcf, discount_factor)
            pv_forecast += present_value
            forecast.append(
                {
                    "year": year,
                    "growth_rate": _s(growth),
                    "fcf": _s(forecast_fcf),
                    "discount_factor": _s(discount_factor),
                    "present_value": _s(present_value),
                }
            )

        terminal_fcf = forecast_fcf * (one + scenario.terminal_growth)
        terminal_value = _div(terminal_fcf, discount_rate - scenario.terminal_growth)
        terminal_discount_factor = _pow(discount_base, len(scenario.growth_rates))
        pv_terminal = _div(terminal_value, terminal_discount_factor)
        intrinsic_value = pv_forecast + pv_terminal

        if scenario.cash_flow_basis == "fcff":
            enterprise_value = intrinsic_value
            equity_value = enterprise_value - scenario.net_debt
            value_basis = "enterprise"
        else:
            equity_value = intrinsic_value
            enterprise_value = equity_value + scenario.net_debt
            value_basis = "equity"

        implied_value_per_share = _div(equity_value, scenario.diluted_shares)

    warnings: list[str] = []
    if scenario.base_fcf <= 0:
        warnings.append("non_positive_base_fcf")
    if scenario.net_debt < 0:
        warnings.append("negative_net_debt_represents_net_cash")
    if scenario.cash_flow_basis == "fcfe" and scenario.net_debt != 0:
        warnings.append("fcfe_values_equity_directly_net_debt_not_subtracted")
    if equity_value <= 0:
        warnings.append("non_positive_implied_equity_value")

    assumptions: dict[str, Any] = {
        "cash_flow_basis": scenario.cash_flow_basis,
        "base_fcf": _s(scenario.base_fcf),
        "growth_rates": [_s(value) for value in scenario.growth_rates],
        "discount_rate_name": discount_rate_name,
        "applied_discount_rate": _s(discount_rate),
        "terminal_growth": _s(scenario.terminal_growth),
        "net_debt": _s(scenario.net_debt),
        "diluted_shares": _s(scenario.diluted_shares),
    }
    if scenario.wacc is not None:
        assumptions["wacc"] = _s(scenario.wacc)
    if scenario.cost_of_equity is not None:
        assumptions["cost_of_equity"] = _s(scenario.cost_of_equity)

    return {
        "method": "dcf_gordon_growth",
        "scenario": scenario.name,
        "currency": scenario.currency,
        "classification": "assumption_based_calculation_not_evidence",
        "value_basis": value_basis,
        "assumptions": assumptions,
        "forecast": forecast,
        "pv_forecast_fcf": _s(pv_forecast),
        "terminal_fcf": _s(terminal_fcf),
        "terminal_value": _s(terminal_value),
        "pv_terminal_value": _s(pv_terminal),
        "enterprise_value": _s(enterprise_value),
        "equity_value": _s(equity_value),
        "implied_value_per_share": _s(implied_value_per_share),
        "warnings": warnings,
    }


def calculate_multiple(scenario: MultipleScenarioAssumption) -> dict[str, Any]:
    """Run a deterministic equity- or enterprise-value multiple calculation."""
    with localcontext() as context:
        context.prec = 34
        basis_value = scenario.metric_value * scenario.multiple
        if scenario.basis == "enterprise":
            enterprise_value = basis_value
            equity_value = enterprise_value - scenario.net_debt
        else:
            equity_value = basis_value
            enterprise_value = equity_value + scenario.net_debt
        implied_value_per_share = _div(equity_value, scenario.diluted_shares)

    warnings: list[str] = []
    if scenario.net_debt < 0:
        warnings.append("negative_net_debt_represents_net_cash")
    if equity_value <= 0:
        warnings.append("non_positive_implied_equity_value")

    return {
        "method": "valuation_multiple",
        "scenario": scenario.name,
        "currency": scenario.currency,
        "classification": "assumption_based_calculation_not_evidence",
        "assumptions": {
            "metric_name": scenario.metric_name,
            "metric_value": _s(scenario.metric_value),
            "multiple": _s(scenario.multiple),
            "basis": scenario.basis,
            "net_debt": _s(scenario.net_debt),
            "diluted_shares": _s(scenario.diluted_shares),
        },
        "basis_value": _s(basis_value),
        "enterprise_value": _s(enterprise_value),
        "equity_value": _s(equity_value),
        "implied_value_per_share": _s(implied_value_per_share),
        "warnings": warnings,
    }


def _latest_market_close(request: AnalysisRequest) -> tuple[str, Decimal] | None:
    massive = request.market_data.get("massive")
    if not isinstance(massive, dict):
        return None
    bars = massive.get("bars")
    if not isinstance(bars, list) or not bars:
        return None
    latest = bars[-1]
    try:
        close = Decimal(str(latest.get("close")))
    except Exception:
        return None
    if close <= 0:
        return None
    return str(latest.get("date")), close


def _add_market_comparison(
    result: dict[str, Any],
    request: AnalysisRequest,
) -> None:
    latest = _latest_market_close(request)
    if latest is None:
        return
    if not request.asset.currency or request.asset.currency.upper() != str(result["currency"]).upper():
        return
    market_date, market_price = latest
    implied = Decimal(str(result["implied_value_per_share"]))
    premium_discount = _div(implied, market_price) - Decimal("1")
    result["market_comparison"] = {
        "market_date": market_date,
        "market_price": _s(market_price),
        "implied_premium_discount": _s(premium_discount),
        "price_source": "market_data.massive.raw_close",
    }


class ValuationCalculationProvider(ResearchDataProvider):
    """Calculate only explicitly supplied valuation scenarios; never invent assumptions."""

    async def enrich(self, request: AnalysisRequest) -> AnalysisRequest:
        assumptions = request.valuation_assumptions
        if assumptions is None or (not assumptions.dcf and not assumptions.multiples):
            return request

        dcf_results = [calculate_dcf(scenario) for scenario in assumptions.dcf]
        multiple_results = [calculate_multiple(scenario) for scenario in assumptions.multiples]
        for result in dcf_results + multiple_results:
            _add_market_comparison(result, request)

        fundamental = dict(request.fundamental_data)
        fundamental["valuation_calculations"] = {
            "classification": "assumption_based_calculations_not_evidence",
            "policy": "explicit_request_assumptions_only",
            "dcf": dcf_results,
            "multiples": multiple_results,
        }
        return request.model_copy(update={"fundamental_data": fundamental})
