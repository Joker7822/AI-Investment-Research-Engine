# Custom LLM fallback

The research engine uses OpenAI as its primary Agents SDK model provider. An operator can configure a second, independent OpenAI-compatible model API for runtime failover.

## Required fallback configuration

Set all three values together:

- `CUSTOM_LLM_BASE_URL` — base URL of the trusted OpenAI-compatible API, normally ending in `/v1`
- `CUSTOM_LLM_API_KEY` — server-side credential for that API
- `CUSTOM_LLM_MODEL` — model name exposed by that API

Optional controls:

- `CUSTOM_LLM_TIMEOUT_SECONDS` — request timeout, default `90`
- `CUSTOM_LLM_FAILOVER_COOLDOWN_SECONDS` — how long to bypass the failing primary after a failover, default `60`

The fallback endpoint must implement the OpenAI-compatible Chat Completions surface used by `OpenAIChatCompletionsModel`, including the structured-output behavior needed by the Agents SDK.

## Failover policy

The same agent prompt and Pydantic output schema are rebuilt against the fallback model when the primary OpenAI request fails because of provider availability, including:

- rate limit or exhausted quota (`429`)
- authentication or permission failure
- connection failure or timeout
- retryable provider/server errors (`408`, `409`, or `5xx`)

Application/schema errors such as a `400` are not automatically retried against the fallback because they usually indicate an invalid request rather than provider unavailability.

After a primary-provider availability failure, a short in-process circuit breaker sends subsequent agent calls directly to the fallback for the configured cooldown. This limits repeated failed OpenAI calls during the multi-agent fan-out.

## Security boundary

The fallback model receives the same model input that would otherwise be sent to the primary provider. Configure only an endpoint that is trusted to process the research request, evidence, filings, and any other context included in model input.

Fallback credentials stay in server-side environment variables. They are not returned by the public API or browser application.

## Production compatibility

Current strict production startup still requires `OPENAI_API_KEY`. The custom provider is a runtime failover for a configured-but-unavailable OpenAI primary. The runtime class itself supports fallback-only operation for development and future deployment modes, with Agents SDK tracing disabled when no OpenAI credential is present.
