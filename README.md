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
- Optional SEC Company Facts enrichment for US equities, filtered by request `as_of`.
- Optional Massive US stock/ETF market-data enrichment with raw OHLCV, dividends, and splits.
- Optional PostgreSQL audit persistence for requests, sources, agent runs, claims, CIO output, and final reports.
- EDINET API v2 daily document-list ingestion into PostgreSQL plus as-of Japanese-equity filing enrichment.

## Point-in-time market-data policy

Historical bars are requested with `adjusted=false`. Vendor-adjusted historical prices can be restated after later splits/dividends and can contaminate historical backtests with information that was not knowable at the analysis date.

The provider stores raw as-traded OHLCV and corporate actions separately. Only completed US sessions are exposed. Dividends require a declaration date already knowable at `as_of` (falling back to ex-date if unavailable), while splits are conservatively included only after execution because the selected split feed does not expose an announcement timestamp.

## EDINET ingestion policy

EDINET's document-list API is date-oriented. The analysis API therefore does **not** scan EDINET historical dates on every user request. Instead:

```text
EDINET API v2 /documents.json (one file date)
  -> edinet-sync job
  -> PostgreSQL edinet_filings index
  -> secCode + submitted_at <= as_of query
  -> EdinetIndexedProvider
  -> EvidenceRef / filings / fundamental_data
```

This makes historical runs reproducible and avoids unnecessary EDINET traffic.

Set `EDINET_API_KEY` and `DATABASE_URL`, then sync a date:

```bash
edinet-sync --date 2026-10-14
```

If `--date` is omitted, the Asia/Tokyo calendar date is used. The sync is idempotent by `doc_id` and records status in `edinet_sync_runs`.

Japanese four-character security codes are normalized to EDINET's five-character `secCode` convention (`7203` -> `72030`, `607A` -> `607A0`). Runtime enrichment queries only filings submitted at or before the request `as_of`; a second in-memory check rejects future records even if the database query were wrong.

The current EDINET milestone indexes **filing metadata** (document ID, issuer, form/document codes, reporting period, XBRL/CSV/PDF availability). It does not yet download and parse the `type=5` XBRL-to-CSV package into normalized financial metrics.

## Audit policy

If `DATABASE_URL` is unset, the app uses a no-op audit repository. If it is configured, the analysis endpoint writes the research provenance to PostgreSQL **before** returning the report.

Database errors are fail-closed: the service does not silently return an unaudited investment report when durable audit storage was explicitly enabled. Reusing the same `request_id` also fails instead of overwriting the original run.

Stored audit data includes request metadata, source IDs/URLs/timestamps/content hashes, specialist/verifier/critic runs, A100 CIO output, claims, direct fact-to-source links where available, and the final report JSON.

## Important limitation

The SEC adapter covers a focused set of US-GAAP metrics. The Massive adapter currently targets US stocks/ETFs and daily data. EDINET currently provides indexed filing metadata, not parsed financial statement values. Consensus, news, options, intraday point-in-time data, and full Japanese XBRL/CSV metric extraction remain production milestones.

The output is a research-system scaffold, not a claim that the service satisfies any financial-regulatory requirement. Personalized investment advice may trigger registration/compliance obligations depending on jurisdiction and product behavior; obtain jurisdiction-specific legal review before deployment.

## Setup

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set `OPENAI_API_KEY` for agent execution. `OPENAI_AGENT_MODEL` is optional.

- `SEC_USER_AGENT`: enables SEC Company Facts enrichment.
- `MASSIVE_API_KEY`: enables US stock/ETF daily market enrichment.
- `DATABASE_URL`: enables audit persistence and the local EDINET filing index.
- `EDINET_API_KEY`: required by the `edinet-sync` ingestion job, not by normal indexed reads.

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
  jobs/
    sync_edinet.py
  orchestration/
    engine.py
    registry.py
    router.py
    scoring.py
  persistence/
    audit.py
    edinet.py
  providers/
    base.py
    edinet.py
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

## Scoring

The deterministic pre-CIO aggregate uses:

```text
score × confidence × evidence_quality × source_independence
```

If multiple agents depend on the same `source_id`, their independence weights are reduced.

## Production next steps

1. Add EDINET `type=5` CSV download/parsing and normalize Japanese financial metrics.
2. Add point-in-time price adjustment / total-return calculations from raw bars + known corporate actions.
3. Add licensed consensus/news/options providers with source-origin clustering.
4. Add strict freshness policies per data type.
5. Add deterministic calculation tools for DCF/multiples instead of asking LLMs to do arithmetic.
6. Add eval datasets and probability-calibration tracking.
7. Add per-agent budget/latency caps, caching, retries, and circuit breakers.
8. Add auth, secret management, data entitlements, and jurisdiction-aware compliance gates.
9. Add proper database migrations/retention policies and production backup/restore procedures.
10. Separate security research from personalized suitability/position-sizing services.

## SDK assumptions verified on 2026-10-14

This scaffold targets `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading pinned dependencies.
