# Deterministic derived financials

Reference date: 2026-10-14.

`DerivedFinancialsProvider` runs after primary-source enrichment and calculates ratios from EDINET Silver metrics with Python `Decimal`, not an LLM.

## Current metrics

- `revenue_growth_yoy`
- `operating_margin`
- `net_margin`
- `operating_cash_flow_margin`
- `asset_growth_yoy`
- `equity_growth_yoy`
- `equity_ratio_calculated`
- `cash_conversion`
- `roa_calculated`
- `roe_calculated`

## Compatibility policy

A calculation is emitted only when its inputs are compatible. The initial policy requires the same:

- EDINET source filing (`doc_id`)
- accounting standard
- consolidated/individual scope
- unit ID

Growth calculations require `CurrentYear` and `Prior1Year` from the same filing. This intentionally favors an issuer's latest restated comparative values instead of mixing a new filing's current value with an older filing's prior-year value.

ROA and ROE use average opening/closing balance-sheet values from `Prior1Year` and `CurrentYear` in the same filing. When equity/assets are non-positive, the arithmetic result is retained for auditability but an economic-instability note is attached.

Zero denominators, missing inputs, source-filing mismatches, scope mismatches and unit mismatches do not produce a numeric output. They are recorded under `fundamental_data.edinet.derived.skipped`.

## Output

Derived metrics are appended at:

```text
fundamental_data.edinet.derived.metrics
```

Every calculation contains the formula and full input provenance inherited from the Silver layer, including `doc_id`, element/context IDs, mapping version, source CSV file and row number.

## Deliberate exclusions

This layer currently does not calculate valuation multiples, FCF, WACC or DCF. Those require additional normalized inputs (e.g. capex, shares/market cap, debt/cash classification, discount-rate assumptions) and will be implemented as separate deterministic calculators rather than inferred by the language model.
