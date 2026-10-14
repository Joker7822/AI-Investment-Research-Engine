# Production deployment on Render

Reference date: 2026-10-14.

The repository includes a root `render.yaml` Blueprint that provisions:

- `investment-research-api`: Docker web service in Singapore.
- `investment-research-db`: private Render Postgres in Singapore.
- `investment-research-edinet-sync`: daily EDINET metadata sync cron job.

The web service uses `/v1/ready` as its deployment health check. Readiness requires:

- PostgreSQL connectivity;
- `OPENAI_API_KEY` configured;
- at least one valid hash in `PUBLIC_API_KEY_HASHES`.

## 1. Generate a public API key

Generate a long random client secret locally, then store only its SHA-256 digest on the server.

Example secret generation:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Hash the generated secret:

```bash
python -c "import hashlib; print(hashlib.sha256(b'PASTE_SECRET_HERE').hexdigest())"
```

Keep the original secret in your client/secret manager. Put only the digest into `PUBLIC_API_KEY_HASHES`.

## 2. Deploy the Blueprint

In Render:

1. Create a new Blueprint.
2. Connect `Joker7822/AI-Investment-Research-Engine`.
3. Use branch `main` and the root `render.yaml`.
4. Review the resources and compute plans before applying the Blueprint.
5. Supply the secret values requested by `sync: false`:
   - `OPENAI_API_KEY`
   - `PUBLIC_API_KEY_HASHES`
   - `SEC_USER_AGENT`
   - `MASSIVE_API_KEY`
   - `EDINET_API_KEY`
6. Deploy.

`SEC_USER_AGENT`, `MASSIVE_API_KEY`, and `EDINET_API_KEY` enable their corresponding data sources. The core public research route still requires `OPENAI_API_KEY`, PostgreSQL, and API-key authentication to pass readiness.

The web service is configured with `autoDeployTrigger: checksPass`, so updates from `main` deploy only after repository checks pass.

## 3. Database migration

Every web deploy runs:

```bash
python -m app.jobs.migrate_db
```

before the new service version receives traffic. The migration executes `db/schema.sql` in a transaction.

The application still retains lazy schema checks as defense in depth, but production deploys should not depend on first-request initialization.

## 4. Health checks

Liveness:

```text
GET /v1/health
```

Readiness:

```text
GET /v1/ready
```

`/v1/ready` returns HTTP `200` only when the database is reachable and required production secrets are configured. It returns `503` otherwise. Secret values are never returned.

## 5. Custom domain

After the Render service is healthy, add a custom domain in the web service settings, for example:

```text
api.example.com
```

Then configure the DNS record requested by Render and complete domain verification. Render manages TLS certificates for verified custom domains and redirects HTTP traffic to HTTPS.

Do not disable the default `onrender.com` address until the custom domain has been verified and tested. After verification, it can be disabled if desired.

## 6. Smoke test

Health:

```bash
curl -i https://YOUR_HOST/v1/health
curl -i https://YOUR_HOST/v1/ready
```

Deterministic valuation (no OpenAI call):

```bash
curl -X POST https://YOUR_HOST/v1/valuation \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_ORIGINAL_SECRET' \
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

Full research:

```bash
curl -X POST https://YOUR_HOST/v1/research \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_ORIGINAL_SECRET' \
  -H 'Idempotency-Key: aapl-20261014-run-001' \
  -d '{
    "as_of":"2026-10-14T12:00:00Z",
    "asset":{"type":"equity","ticker":"AAPL","exchange":"NASDAQ","currency":"USD","region":"US"},
    "user_question":"Analyze Apple with a 12 month horizon",
    "investment_horizon":"12m"
  }'
```

## 7. EDINET schedule

The Blueprint runs `edinet-sync` daily at `09:30 UTC`, which is `18:30 JST`. Render cron schedules are evaluated in UTC.

The sync job writes to the same PostgreSQL database as the API. The current cron synchronizes EDINET filing metadata; `type=5` CSV packages remain a separate ingestion step.

## 8. Scaling notes

The default Blueprint uses one Uvicorn worker and asynchronous agent execution. Start with one worker so request-level concurrency, database pools, and agent parallelism remain easy to reason about.

Scale in this order after measuring production traffic:

1. tune `MAX_PARALLEL_AGENTS`;
2. increase web-service CPU/RAM;
3. increase `WEB_CONCURRENCY` only after validating database and upstream API limits;
4. move long research executions to an asynchronous job/worker architecture if request duration becomes a reliability constraint.

The Blueprint intentionally uses paid production-class compute IDs rather than an expiring free Postgres database. Review current Render pricing before applying it.
