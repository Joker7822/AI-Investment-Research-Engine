# Investment Research API v1

Reference date: 2026-10-14.

This API exposes the investment-research engine as a versioned REST service. The public contract keeps observed evidence, deterministic calculations, and explicit valuation assumptions separate.

## Authentication

Set `PUBLIC_API_KEY_HASHES` to one or more comma-separated SHA-256 hashes. Clients send the original secret in:

```http
X-API-Key: <secret>
```

Only hashes are configured on the server. If no hashes are configured, authentication is disabled for local development.

## Correlation IDs

Clients may send:

```http
X-Request-ID: client-correlation-id
```

The API echoes it in the response. If omitted, the API generates one. This is an HTTP correlation ID and is separate from the investment research `request_id`.

## POST /v1/research

Runs the full point-in-time enrichment + 100-agent orchestration pipeline.

Production requirements:

- `OPENAI_API_KEY`
- `DATABASE_URL`
- `Idempotency-Key` header
- `X-API-Key` when authentication is enabled

Example:

```bash
curl -X POST http://localhost:8000/v1/research \
  -H 'content-type: application/json' \
  -H 'X-API-Key: your-secret-key' \
  -H 'Idempotency-Key: research-aapl-20261014-001' \
  -d '{
    "as_of":"2026-10-14T12:00:00Z",
    "asset":{"type":"equity","ticker":"AAPL","exchange":"NASDAQ","currency":"USD","region":"US"},
    "user_question":"Analyze Apple with a 12 month horizon",
    "investment_horizon":"12m"
  }'
```

Successful response:

```json
{
  "status": "completed",
  "request_id": "req_...",
  "report": {"...": "InvestmentReport"},
  "idempotent_replay": false
}
```

### Idempotency behavior

The server stores only a SHA-256 hash of `Idempotency-Key`.

- Same key + same payload + completed request: returns the stored report with `idempotent_replay=true`.
- Same key + different payload: `409 idempotency_key_reused`.
- Same key while original request is still running: `409 request_in_progress`.
- Failed run before audit persistence: the reservation is released, allowing a retry.
- If audit persistence succeeds but idempotency completion is interrupted, a later retry repairs the state from the stored report.

The semantic payload hash intentionally excludes `request_id`; the idempotency key identifies the operation rather than a caller-selected report identifier.

## GET /v1/reports/{request_id}

Returns a persisted report. Requires PostgreSQL audit storage.

```bash
curl http://localhost:8000/v1/reports/req_example \
  -H 'X-API-Key: your-secret-key'
```

## POST /v1/valuation

Runs only deterministic DCF/multiple calculations. It does **not** call OpenAI.

The request must contain at least one explicit scenario. Missing WACC, FCF, growth, multiple, net debt, or diluted shares are never invented.

```bash
curl -X POST http://localhost:8000/v1/valuation \
  -H 'content-type: application/json' \
  -H 'X-API-Key: your-secret-key' \
  -d '{
    "as_of":"2026-10-14T12:00:00Z",
    "asset":{"type":"equity","ticker":"TEST","currency":"USD","region":"US"},
    "assumptions":{
      "dcf":[{
        "name":"base",
        "currency":"USD",
        "base_fcf":"1000000000",
        "growth_rates":["0.08","0.07","0.06","0.05","0.04"],
        "wacc":"0.085",
        "terminal_growth":"0.025",
        "net_debt":"200000000",
        "diluted_shares":"100000000"
      }],
      "multiples":[]
    }
  }'
```

If Massive market data is configured and the asset/currency is compatible, the response can also contain an as-of-safe market-price comparison.

## POST /v1/route

Returns the deterministic agent route without running any LLM calls. Useful for cost/coverage previews.

## GET /v1/health

Public liveness/configuration summary. It never returns secrets.

## Error format

HTTP errors and validation errors use one envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "request_id": "http_...",
    "details": []
  }
}
```

Unhandled exceptions return `500 internal_error` without stack traces or secrets.

## OpenAPI

When `API_DOCS_ENABLED=true`:

- `/docs` - Swagger UI
- `/redoc` - ReDoc
- `/openapi.json` - machine-readable schema

The schema includes `X-API-Key` as an API-key security scheme.

## Deployment boundary

`POST /v1/research` intentionally fails closed without PostgreSQL. The public research endpoint promises immutable audit provenance and idempotency, so it does not silently downgrade to an unaudited mode. The legacy `/v1/analyze` endpoint remains for backward compatibility and is marked deprecated in OpenAPI.
