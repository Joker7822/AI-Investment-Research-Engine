# AI Investment Research Engine — 100-Agent Architecture

A point-in-time investment research engine with **100 registered specialist roles**, deterministic routing, primary-source evidence, auditable financial normalization, explicit valuation assumptions, and an authenticated REST API.

**Reference date:** 2026-10-14.

## Architecture

```text
Client
  -> REST API / API-key auth / idempotency
  -> point-in-time data enrichment
     -> SEC Company Facts
     -> EDINET filing index -> type=5 Bronze -> Silver
     -> raw US market OHLCV / corporate actions
  -> deterministic derived financials
  -> explicit-assumption DCF / multiples
  -> deterministic Router
  -> bounded parallel specialists
  -> verification
  -> Bull / Bear / Thesis Breaker / Evidence Judge / Risk Committee
  -> A100 CIO
  -> deterministic scoring
  -> PostgreSQL audit trail
  -> InvestmentReport
```

The system intentionally does **not** send every question to all 100 agents. Python owns orchestration, arithmetic, point-in-time filtering, source provenance, and final numeric score aggregation.

## Current implementation

- A01–A100 agent registry and deterministic rules-first routing.
- Parallel specialists -> verification -> critics/risk committee -> A100 CIO.
- OpenAI Agents SDK structured outputs and tracing.
- Evidence-source independence penalty in deterministic score aggregation.
- SEC Company Facts enrichment for US equities with `as_of` controls.
- Massive raw US daily OHLCV + dividend/split enrichment with point-in-time controls.
- PostgreSQL immutable request/report/agent/evidence audit persistence.
- Public API idempotency using hashed `Idempotency-Key` values.
- EDINET API v2 daily filing index with Japanese-equity `as_of` lookup.
- EDINET `type=5` XBRL-to-CSV Bronze ingestion preserving official source rows.
- Deterministic EDINET Silver normalization for 12 core financial metrics across JP GAAP / IFRS / US GAAP starter mappings.
- Ten deterministic derived financial ratios using `Decimal` arithmetic.
- Explicit-assumption deterministic DCF and valuation-multiple calculators.
- Authenticated REST API with `X-API-Key`, correlation IDs, Swagger/OpenAPI, and immutable report retrieval.
- Docker production image, Render Blueprint, pre-deploy migration, readiness checks, and EDINET daily sync.
- CI on Python 3.11 and 3.12.

## Public REST API

Primary endpoints:

```text
POST /v1/research
POST /v1/valuation
GET  /v1/reports/{request_id}
POST /v1/route
GET  /v1/health
GET  /v1/ready
```

`POST /v1/research` requires durable PostgreSQL audit/idempotency storage and an `Idempotency-Key`. `POST /v1/valuation` performs deterministic valuation only and does not call OpenAI.

See [`docs/public-api.md`](docs/public-api.md).

## Point-in-time policy

Historical market bars are requested with `adjusted=false`. Raw as-traded OHLCV and corporate actions remain separate so later splits/dividends do not silently rewrite historical backtests.

SEC and EDINET facts are filtered by the request's `as_of` timestamp. EDINET runtime lookup enforces `submitted_at <= as_of` in SQL and again in memory.

## EDINET pipeline

```text
EDINET /documents.json
  -> edinet-sync
  -> edinet_filings
  -> type=5 ZIP
  -> edinet_csv_facts (Bronze)
  -> exact/versioned element mapping
  -> edinet_silver_metrics (Silver)
  -> as-of runtime selection
  -> fundamental_data.edinet.metrics
```

Sync filing metadata:

```bash
edinet-sync --date 2026-10-14
```

Fetch one indexed type=5 package:

```bash
edinet-csv-sync --doc-id S100XXXX
```

The Silver layer uses explicit element IDs and strict annual contexts. Issuer-specific extension elements are not fuzzily guessed; unmatched data remains available in Bronze for review.

## Deterministic financial calculations

Derived EDINET metrics currently include:

```text
revenue_growth_yoy
operating_margin
net_margin
operating_cash_flow_margin
asset_growth_yoy
equity_growth_yoy
equity_ratio_calculated
cash_conversion
roa_calculated
roe_calculated
```

Calculations refuse to mix incompatible filings, accounting standards, scopes, or units.

Valuation supports explicit-assumption:

- Gordon-growth DCF;
- equity-value multiples;
- enterprise-value multiples;
- optional point-in-time market-price premium/discount comparison.

The model is not allowed to invent missing WACC, growth, FCF, peer multiple, net debt, or diluted-share assumptions and present them as facts.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Important settings:

- `OPENAI_API_KEY`: agent execution.
- `PUBLIC_API_KEY_HASHES`: SHA-256 hashes accepted by `X-API-Key`.
- `DATABASE_URL`: audit persistence, idempotency, and EDINET storage.
- `SEC_USER_AGENT`: SEC Company Facts enrichment.
- `MASSIVE_API_KEY`: US market data.
- `EDINET_API_KEY`: EDINET ingestion jobs.

Generate an API secret and hash:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import hashlib; print(hashlib.sha256(b'PASTE_SECRET_HERE').hexdigest())"
```

## Run locally

```bash
./scripts/start-api.sh
```

or:

```bash
uvicorn app.main:app --reload
```

Health:

```bash
curl http://localhost:8000/v1/health
```

Deterministic route preview:

```bash
curl -s -X POST http://localhost:8000/v1/route \
  -H 'content-type: application/json' \
  -H 'X-API-Key: YOUR_SECRET' \
  --data @sample_request.json
```

Full public research:

```bash
curl -s -X POST http://localhost:8000/v1/research \
  -H 'content-type: application/json' \
  -H 'X-API-Key: YOUR_SECRET' \
  -H 'Idempotency-Key: sample-run-001' \
  --data @sample_request.json
```

`POST /v1/analyze` remains as a deprecated compatibility endpoint.

## Production deployment

The root [`render.yaml`](render.yaml) provisions a Singapore-region Docker web service, Render Postgres, and an EDINET daily sync cron job.

Deployment controls include:

- non-root Docker runtime;
- `$PORT` support;
- pre-deploy schema migration;
- `/v1/ready` database/config readiness probe;
- `autoDeployTrigger: checksPass`;
- graceful shutdown allowance for long-running research requests;
- private Postgres ingress;
- EDINET schedule at `09:30 UTC` (`18:30 JST`).

See [`docs/deployment-render.md`](docs/deployment-render.md) before applying the Blueprint. Review the configured Render compute plans and current pricing first.

## Audit policy

Public research is fail-closed. If PostgreSQL audit/idempotency storage is unavailable, `POST /v1/research` does not silently downgrade to an unaudited run.

The audit trail stores the complete enriched request JSON, source provenance, agent outputs, CIO judgement, valuation assumptions/results, and final report so historical decisions can be reconstructed.

## Directory layout

```text
app/
  api/
  jobs/
    migrate_db.py
    sync_edinet.py
    sync_edinet_csv.py
  metrics/
  orchestration/
  persistence/
  providers/
config/
  agents.yaml
  edinet_metrics.yaml
db/
  schema.sql
docs/
  public-api.md
  deployment-render.md
scripts/
  start-api.sh
render.yaml
Dockerfile
tests/
```

## Scoring

The deterministic pre-CIO aggregate uses:

```text
score × confidence × evidence_quality × source_independence
```

## Production next steps

1. Expand reviewed/versioned EDINET mappings and issuer-extension coverage.
2. Add point-in-time Japanese market prices and total-return reconstruction.
3. Add licensed consensus, news, options, short-interest, and estimate-revision providers.
4. Build WACC/FCF normalization and scenario-generation policies with explicit provenance.
5. Add backtests, calibration datasets, model/agent scorecards, and regression gates.
6. Move long research runs to an asynchronous job API when synchronous request duration becomes an operational constraint.
7. Add rate limits, tenant-level API keys/quotas, billing hooks, secret rotation, migrations, retention policies, and backup/restore drills.
8. Complete jurisdiction-specific financial-regulatory and suitability review before personalized advice is offered.

## SDK assumption

The project currently pins `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading dependencies.
