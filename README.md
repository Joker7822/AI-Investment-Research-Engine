# Investment AI — 100-Agent Architecture

A runnable MVP scaffold for a hierarchical investment-research system with **100 registered agent roles** and deterministic orchestration.

**Reference date:** 2026-10-14.

## Why this architecture

The system intentionally avoids sending every question to all 100 agents. Python owns the workflow:

1. Normalize the request and evidence.
2. Deterministically route to a bounded specialist set.
3. Run specialists in parallel.
4. Run verification agents against specialist outputs.
5. Run critics / risk committee against verified context.
6. Let A100 CIO synthesize only qualitative judgement fields; Python owns numeric scoring and execution metadata.

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

## Important limitation

This repository does **not** ship a live market-data vendor. `PassthroughDataProvider` only analyzes data supplied by the caller. Do not treat an empty-data run as investment research. Add licensed/authorized market, filing, consensus, and news providers before production use.

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
  providers/
    base.py
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
  -> Specialists (bounded, parallel)
  -> A81/A82/A83/A89/A90 verification
  -> A91/A92/A94/A98/A99 committee
  -> A100 CIO
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

1. Wire `db/schema.sql` through a persistence repository and store immutable request/run/source provenance.
2. Replace `PassthroughDataProvider` with normalized, timestamped data adapters.
3. Create production evidence ingestion and source-origin clustering with immutable source provenance.
4. Add strict freshness policies per data type.
5. Add calculation tools for DCF/multiples instead of asking LLMs to do arithmetic.
6. Add eval datasets and calibration tracking.
7. Add per-agent budget/latency caps, caching, retries, and circuit breakers.
8. Add auth, audit logs, secret management, data entitlements, and jurisdiction-aware compliance gates.
9. Separate security research from personalized suitability/position-sizing services.

## SDK assumptions verified on 2026-10-14

This scaffold targets `openai-agents==0.23.1`. Re-check official SDK documentation before upgrading pinned dependencies.
