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

- A01–A100 agent registry and deterministic routing.
- Parallel specialists -> verification -> critics/risk committee -> A100 CIO.
- Structured outputs, source-independence scoring, tracing, FastAPI, Docker and CI.
- SEC Company Facts enrichment for US equities with `as_of` controls.
- Massive US daily raw OHLCV + dividend/split enrichment with point-in-time controls.
- PostgreSQL immutable audit persistence.
- EDINET API v2 daily filing index with Japanese-equity `as_of` lookup.
- EDINET `type=5` XBRL-to-CSV Bronze ingestion preserving the official nine-column source rows.
- Deterministic EDINET Silver normalization for 12 core financial metrics across JP GAAP / IFRS / US GAAP starter mappings.

## Point-in-time market-data policy

Historical bars are requested with `adjusted=false`. Vendor-adjusted historical prices can be restated after later splits/dividends and can contaminate historical backtests with information that was not knowable at the analysis date.

Raw as-traded OHLCV and corporate actions remain separate. Only completed US sessions are exposed. Dividends require a declaration date already knowable at `as_of` (falling back to ex-date if unavailable); splits are conservatively included only after execution because the selected split feed does not expose an announcement timestamp.

## EDINET ingestion policy

The date-oriented EDINET document-list API is synchronized into PostgreSQL rather than scanned repeatedly during user requests:

```text
EDINET /documents.json
  -> edinet-sync
  -> edinet_filings
  -> secCode + submitted_at <= as_of
  -> EdinetIndexedProvider
```

Sync one date:

```bash
edinet-sync --date 2026-10-14
```

Japanese four-character security codes are normalized to EDINET's five-character `secCode` convention (`7203` -> `72030`, `607A` -> `607A0`). Runtime enrichment enforces `submitted_at <= as_of` in SQL and again in memory.

### EDINET type=5 Bronze layer

For an indexed filing with `csvFlag=1` and a fetchable `legalStatus`, download and store its XBRL-to-CSV package:

```bash
edinet-csv-sync --doc-id S100XXXX
```

The Bronze parser never extracts ZIP files to disk, accepts only `XBRL_TO_CSV/*.csv`, validates the official nine-column header, decodes UTF-16/UTF-16LE, supports quoted multiline values, imposes archive/row limits, and hashes the package with SHA-256.

Every original row is retained with:

```text
要素ID / 項目名 / コンテキストID / 相対年度 / 連結・個別 /
期間・時点 / ユニットID / 単位 / 値
```

### EDINET Silver layer

`config/edinet_metrics.yaml` is a versioned exact-element mapping. The initial `2026-10-14-v1` map covers:

```text
revenue
operating_income
ordinary_income
profit_before_tax
net_income
total_assets
net_assets
eps
roe_official
equity_ratio_official
cf_operating
cash
```

The extractor does **not** map by Japanese label similarity or fuzzy regex. It accepts only explicitly configured element IDs and exact annual contexts such as `CurrentYearDuration`, `Prior1YearDuration`, `CurrentYearInstant`, and their `_NonConsolidatedMember` variants. Segment/member contexts are ignored.

Accounting standard is detected separately for JP GAAP, IFRS, and US GAAP. Duration metrics cannot consume instant contexts and vice versa. Consolidated and individual facts remain distinguishable.

Every Silver value retains:

```text
mapping_version
accounting_standard
mapping_priority
scope
period_key
element_id
context_id
unit
raw_value
numeric_value
doc_id
source_file
row_number
```

Bronze and Silver are committed in one PostgreSQL transaction. Even if the source ZIP SHA-256 is unchanged, Silver is rebuilt so a mapping-version update can be applied without changing or rewriting the underlying source package.

At analysis time, the newest as-of-safe EDINET filing wins first; within that filing the runtime prefers consolidated scope and then the mapping's explicit element priority. The selected values and full provenance are placed under:

```text
fundamental_data.edinet.metrics
```

for A11–A30 and verification agents.

**Current limitation:** issuer-specific extension elements are intentionally not guessed. They remain in Bronze until an auditable explicit mapping is added.

## Audit policy

If `DATABASE_URL` is unset, the app uses a no-op audit repository. If configured, the analysis endpoint writes research provenance to PostgreSQL before returning a report. Database errors are fail-closed, and duplicate `request_id` values do not overwrite prior runs.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Key settings:

- `OPENAI_API_KEY`: agent execution.
- `SEC_USER_AGENT`: SEC Company Facts enrichment.
- `MASSIVE_API_KEY`: US daily market enrichment.
- `DATABASE_URL`: audit persistence and EDINET index/Bronze/Silver storage.
- `EDINET_API_KEY`: `edinet-sync` and `edinet-csv-sync` ingestion jobs.

## Run

```bash
uvicorn app.main:app --reload
```

Route only:

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
  jobs/
    sync_edinet.py
    sync_edinet_csv.py
  metrics/
    edinet_silver.py
  orchestration/
  persistence/
    audit.py
    edinet.py
  providers/
    base.py
    edinet.py
    edinet_csv.py
    massive.py
    sec.py
config/
  agents.yaml
  edinet_metrics.yaml
db/schema.sql
tests/
```

## Scoring

The deterministic pre-CIO aggregate uses:

```text
score × confidence × evidence_quality × source_independence
```

## Production next steps

1. Expand the Silver mapping from the 12 core metrics using current EDINET taxonomy lists and explicit issuer-extension mappings with review/versioning.
2. Add deterministic derived metrics: growth, margins, FCF, ROIC/ROE checks, leverage, and quality flags.
3. Add deterministic DCF/multiple calculators so valuation agents do not perform arithmetic in free-form text.
4. Add point-in-time total-return calculations from raw bars + known corporate actions.
5. Add licensed consensus/news/options providers with source-origin clustering.
6. Add calibration/eval datasets, per-agent budgets, caching, retries, circuit breakers, auth, entitlements, migrations and backup/restore.

## SDK assumptions verified on 2026-10-14

This scaffold targets `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading pinned dependencies.
