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
- EDINET `type=5` XBRL-to-CSV Bronze ingestion: official nine-column UTF-16LE TSV is parsed and stored without semantic remapping.

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

The package is fetched with EDINET document API `type=5`. The parser:

- never extracts ZIP files to disk;
- accepts only `XBRL_TO_CSV/*.csv` members;
- validates the official nine-column header exactly;
- decodes UTF-16/UTF-16LE and parses tab-delimited quoted values, including multiline text blocks;
- applies file-count, per-file, total-uncompressed-size and row-count limits;
- calculates a SHA-256 for package provenance.

The Bronze table preserves, for every row:

```text
要素ID / 項目名 / コンテキストID / 相対年度 / 連結・個別 /
期間・時点 / ユニットID / 単位 / 値
```

`edinet_csv_packages` stores package hash/file/fact counts; `edinet_csv_facts` stores the raw canonical rows. If the same `doc_id` and package SHA-256 are synchronized again, no rewrite occurs. Changed packages are replaced atomically using PostgreSQL COPY for the bulk facts.

Semantic metric mapping is intentionally a separate Silver layer. The Bronze parser does **not** guess that a label means revenue/profit/etc.

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
- `DATABASE_URL`: audit persistence and EDINET index/Bronze storage.
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
config/agents.yaml
db/schema.sql
tests/
```

## Scoring

The deterministic pre-CIO aggregate uses:

```text
score × confidence × evidence_quality × source_independence
```

## Production next steps

1. Build an EDINET Silver layer that maps official/extension element IDs into auditable Japanese financial metrics without losing Bronze provenance.
2. Feed normalized EDINET metrics into `fundamental_data` and A11–A30 agents.
3. Add point-in-time total-return calculations from raw bars + known corporate actions.
4. Add licensed consensus/news/options providers with source-origin clustering.
5. Add deterministic DCF/multiple calculation tools and calibration/eval datasets.
6. Add per-agent budgets, caching, retries, circuit breakers, auth, entitlements, migrations and backup/restore.

## SDK assumptions verified on 2026-10-14

This scaffold targets `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading pinned dependencies.
