# Deterministic valuation calculators

Reference date: 2026-10-14.

The language model does not perform DCF or multiple arithmetic. Valuation scenarios must be explicitly supplied in `AnalysisRequest.valuation_assumptions`; `ValuationCalculationProvider` calculates them before A21–A30 run.

## Evidence boundary

`valuation_assumptions` and `fundamental_data.valuation_calculations` are classified as **assumption-based scenario data, not observed evidence**. They do not create `EvidenceRef` objects or source IDs automatically.

The enriched request, including all assumptions and deterministic results, is persisted in `analysis_requests.request_json` when audit persistence is enabled.

## DCF

DCF scenarios must explicitly identify their cash-flow basis.

### FCFF

Free cash flow to the firm is discounted at WACC and produces enterprise value first:

```text
FCFF_t = FCFF_(t-1) × (1 + growth_t)
PV(FCFF_t) = FCFF_t / (1 + WACC)^t
Terminal FCFF = final forecast FCFF × (1 + terminal growth)
Terminal Value = Terminal FCFF / (WACC - terminal growth)
Enterprise Value = Σ PV(FCFF_t) + PV(Terminal Value)
Equity Value = Enterprise Value - Net Debt
Implied Value Per Share = Equity Value / Diluted Shares
```

Use:

```json
{
  "cash_flow_basis": "fcff",
  "wacc": "0.085"
}
```

`cost_of_equity` must not be supplied for an FCFF scenario.

### FCFE

Free cash flow to equity is discounted at cost of equity and produces equity value directly:

```text
FCFE_t = FCFE_(t-1) × (1 + growth_t)
PV(FCFE_t) = FCFE_t / (1 + Cost of Equity)^t
Terminal FCFE = final forecast FCFE × (1 + terminal growth)
Terminal Value = Terminal FCFE / (Cost of Equity - terminal growth)
Equity Value = Σ PV(FCFE_t) + PV(Terminal Value)
Enterprise Value = Equity Value + Net Debt
Implied Value Per Share = Equity Value / Diluted Shares
```

Use:

```json
{
  "cash_flow_basis": "fcfe",
  "cost_of_equity": "0.10"
}
```

`wacc` must not be supplied for an FCFE scenario. Net debt is **not subtracted** from the FCFE intrinsic value because FCFE already values equity directly. If net debt is supplied, it is used only to derive an informational enterprise value.

For backward compatibility, omitted `cash_flow_basis` defaults to `fcff`; therefore an omitted basis still requires `wacc`.

Validation requires:

- 1–20 explicit forecast growth rates;
- FCFF: `WACC > terminal growth`;
- FCFE: `cost of equity > terminal growth`;
- the applicable discount rate, terminal growth, and forecast growth greater than -100%;
- diluted shares > 0;
- no mixed WACC/cost-of-equity inputs for the wrong cash-flow basis.

Negative base FCF is mathematically allowed but flagged. Negative net debt is interpreted as net cash and flagged for clarity.

## Multiples

A multiple scenario explicitly supplies:

- metric name;
- positive metric value;
- positive multiple;
- valuation basis (`equity` or `enterprise`);
- net debt;
- diluted shares;
- currency.

For enterprise-value multiples:

```text
Enterprise Value = metric × multiple
Equity Value = Enterprise Value - Net Debt
```

For equity-value multiples:

```text
Equity Value = metric × multiple
Enterprise Value = Equity Value + Net Debt
```

Both produce implied value per share.

## Market comparison

When Massive raw market data is present and its asset currency matches the valuation scenario currency, Python compares implied value per share against the latest as-of-safe raw close:

```text
implied premium/discount = implied value per share / market price - 1
```

No comparison is emitted for a currency mismatch or missing/invalid price.

## Audit / replay

The database schema stores the complete enriched request JSON. This is required because a valuation result cannot be reproduced from the final report alone: the exact cash-flow basis, discount rate, growth path, terminal growth, net debt, share count and multiple assumptions must be recoverable.

## Deliberate limitations

The current calculators do not infer assumptions, calculate WACC/cost of equity, normalize FCFF/FCFE, estimate diluted shares, or select peer multiples. Those remain separate research tasks. The model may discuss supplied assumptions and sensitivity risk, but it must not invent missing assumptions and present them as data.
