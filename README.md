# Investment AI — 100-Agent Architecture

A runnable MVP scaffold for a hierarchical investment-research system with **100 registered agent roles** and deterministic orchestration.

**Reference date:** 2026-10-14.

## Why this architecture

The system intentionally avoids sending every question to all 100 agents. Python owns the workflow:

1. Normalize the request and evidence.
2. Enrich with configured data providers and enforce point-in-time rules.
3. Deterministically route to a bounded specialist set.
4. Run specialists in parallel.
5. Run verification agents against specialist outputs.
6. Run critics / risk committee against verified context.
7. Let A100 CIO synthesize only qualitative judgement fields; Python owns numeric scoring and execution metadata.
8. Persist the enriched request, source provenance, agent outputs, CIO judgement, and final report when PostgreSQL audit storage is enabled.

This keeps one final answer owner while reducing non-deterministic routing, duplicated evidence, latency, and cost.

## Current implementation

- `config/agents.yaml`: A01–A100 registry.
- `DeterministicRouter`: rules-first routing, with mandatory verification/committee agents.
- `InvestmentAnalysisEngine`: parallel specialists, then verification, critics, then CIO.
- Pydantic structured outputs via `Agent.output_type`.
- Evidence-source independence penalty in score aggregation.
- A62 Position Sizing is blocked unless `risk_profile_available=true`.
- FastAPI endpoints for routing and analysis.
- OpenAI Agents SDK tracing wraps the end-to-end workflow.
- GitHub Actions CI on Python 3.11 and 3.12.
- Optional SEC Company Facts enrichment for US equities, filtered strictly by the request `as_of` timestamp.
- Optional Massive US stock/ETF market-data enrichment with raw OHLCV, dividends, and splits.
- Optional PostgreSQL audit persistence for requests, sources, agent runs, claims, CIO output, and final reports.

## Point-in-time market-data policy

Historical bars are requested with `adjusted=false`. This is deliberate: vendor-adjusted historical prices can be restated after later splits/dividends and can contaminate historical backtests with information that was not knowable at the analysis date.

The provider therefore stores:

- raw as-traded daily OHLCV;
- only completed US sessions as of the request timestamp;
- dividends only once their declaration date is knowable (falling back to ex-date if declaration date is unavailable);
- splits only after their execution date because the selected split feed does not expose an announcement timestamp.

Corporate actions remain separate from raw prices so a future point-in-time adjustment engine can reconstruct adjusted returns using only information available at each historical timestamp.

## Audit policy

If `DATABASE_URL` is unset, the app uses a no-op audit repository. If `DATABASE_URL` is configured, the analysis endpoint writes the entire research provenance to PostgreSQL **before** returning the report.

Database errors are fail-closed by design: the service does not silently return an unaudited investment report when durable audit storage was explicitly enabled. Reusing the same `request_id` also fails instead of overwriting the original run, preserving immutability.

The PostgreSQL schema stores:

- analysis request metadata;
- source IDs, source URLs, timestamps, quality scores, and content hashes;
- each specialist/verifier/critic run and the A100 CIO judgement;
- facts, analysis claims, risks, invalidators, and direct fact-to-source links where available;
- the final report JSON.

## Important limitation

The SEC adapter covers a focused set of primary-source US-GAAP metrics. The Massive adapter currently targets US stocks/ETFs and daily data. Consensus, news, options, intraday point-in-time data, and non-US market/filing providers still need licensed/authorized adapters before production use.

The output is a research-system scaffold, not a claim that the service satisfies any financial-regulatory requirement. Personalized investment advice may trigger registration/compliance obligations depending on jurisdiction and product behavior; obtain jurisdiction-specific legal review before deployment.

## Setup

Python 3.11+ is recommended for this scaffold.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env` or the environment. The app passes it to the Agents SDK before the first model call.

`OPENAI_AGENT_MODEL` is optional. If blank, the SDK's default model/provider configuration is used.

For US-equity SEC enrichment, set `SEC_USER_AGENT` to an application/contact identifier. SEC facts filed after the request `as_of` time are excluded.

For US stock/ETF market enrichment, set `MASSIVE_API_KEY`. `MASSIVE_LOOKBACK_DAYS` defaults to 400 calendar days.

For durable audit storage, set `DATABASE_URL` to a PostgreSQL DSN. The repository initializes `db/schema.sql` lazily on the first persisted analysis.

## Run

```bash
uvicorn app.main:app --reload
```

Health:

```bash
curl http://localhost:8000/health
```

Route only (no LLM call):

```bash
curl -s -X POST http://localhost:8000/v1/route \
  -H 'content-type: application/json' \
  --data @sample_request.json
```

Full analysis:

```bash
curl -s -X POST http://localhost:8000/v1/analyze \
  -H 'content-type: application/json' \
  --data @sample_request.json
```

## Directory layout

```text
app/
  api/main.py
  core/settings.py
  domain/
    prompts.py
    schemas.py
  orchestration/
    engine.py
    registry.py
    router.py
    scoring.py
  persistence/
    audit.py
  providers/
    base.py
    massive.py
    sec.py
config/
  agents.yaml
db/
  schema.sql
docs/
  architecture.md
tests/
```

## Routing stages

```text
Request
  -> Data enrichment / as-of filtering
  -> Specialists (bounded, parallel)
  -> A81/A82/A83/A89/A90 verification
  -> A91/A92/A94/A98/A99 committee
  -> A100 CIO
  -> Audit persistence (when configured)
  -> InvestmentReport
```

The router can later add an LLM relevance ranker, but it must never remove mandatory verification and risk-control stages.

## Scoring

The deterministic pre-CIO aggregate uses:

```text
score × confidence × evidence_quality × source_independence
```

If multiple agents depend on the same `source_id`, their independence weights are reduced. This is a simple first defense against an AI echo chamber; production should move to an explicit claim/evidence graph and cluster sources by underlying origin, not just string ID.

## Production next steps

1. Add an EDINET daily filing index and Japanese primary-filing provider.
2. Add point-in-time price adjustment / total-return calculations from raw bars + known corporate actions.
3. Add licensed consensus/news/options providers with source-origin clustering.
4. Add strict freshness policies per data type.
5. Add deterministic calculation tools for DCF/multiples instead of asking LLMs to do arithmetic.
6. Add eval datasets and calibration tracking.
7. Add per-agent budget/latency caps, caching, retries, and circuit breakers.
8. Add auth, secret management, data entitlements, and jurisdiction-aware compliance gates.
9. Add database migrations/retention policies and production backup/restore procedures.
10. Separate security research from personalized suitability/position-sizing services.

## SDK assumptions verified on 2026-10-14

This scaffold targets `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading pinned dependencies.
