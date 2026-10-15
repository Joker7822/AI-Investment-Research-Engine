from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, RateLimitError

from app.core.settings import Settings
from app.orchestration import model_runtime as runtime_module
from app.orchestration.model_runtime import (
    ModelRuntime,
    custom_llm_configured,
    model_runtime_configured,
    primary_provider_unavailable,
)


def _settings(**overrides) -> Settings:
    values = {
        "openai_api_key": "primary-secret",
        "custom_llm_base_url": "https://llm.example.test/v1",
        "custom_llm_api_key": "fallback-secret",
        "custom_llm_model": "independent-model",
        "custom_llm_failover_cooldown_seconds": 60,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(429, request=request)
    return RateLimitError(
        "quota exhausted",
        response=response,
        body={"error": {"code": "insufficient_quota"}},
    )


def _bad_request_error() -> BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(400, request=request)
    return BadRequestError(
        "invalid request",
        response=response,
        body={"error": {"code": "invalid_request_error"}},
    )


def test_custom_llm_configuration_is_all_or_nothing():
    partial = Settings(
        _env_file=None,
        custom_llm_base_url="https://llm.example.test/v1",
    )
    with pytest.raises(ValueError, match="must be configured together"):
        custom_llm_configured(partial)

    fallback_only = _settings(openai_api_key=None)
    assert custom_llm_configured(fallback_only) is True
    assert model_runtime_configured(fallback_only) is True


def test_primary_unavailable_classification_does_not_retry_bad_requests():
    assert primary_provider_unavailable(_rate_limit_error()) is True
    assert primary_provider_unavailable(_bad_request_error()) is False


@pytest.mark.asyncio
async def test_runtime_fails_over_after_primary_rate_limit_and_opens_circuit(monkeypatch):
    settings = _settings()
    runtime = ModelRuntime(settings)
    calls: list[object] = []

    async def fake_run(agent, input_text):
        calls.append(agent)
        if len(calls) == 1:
            raise _rate_limit_error()
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    first = await runtime.run(build_agent, "payload")
    second = await runtime.run(build_agent, "payload-2")

    assert first.final_output == "ok"
    assert second.final_output == "ok"
    assert calls == ["primary", "fallback", "fallback"]


@pytest.mark.asyncio
async def test_runtime_keeps_primary_when_primary_succeeds(monkeypatch):
    runtime = ModelRuntime(_settings())
    calls: list[object] = []

    async def fake_run(agent, input_text):
        calls.append(agent)
        return SimpleNamespace(final_output="primary-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    result = await runtime.run(build_agent, "payload")
    assert result.final_output == "primary-ok"
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_fallback_only_runtime_never_calls_primary(monkeypatch):
    monkeypatch.setattr(runtime_module, "set_tracing_disabled", lambda **kwargs: None)
    runtime = ModelRuntime(_settings(openai_api_key=None))
    calls: list[object] = []

    async def fake_run(agent, input_text):
        calls.append(agent)
        return SimpleNamespace(final_output="fallback-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    result = await runtime.run(build_agent, "payload")
    assert result.final_output == "fallback-ok"
    assert calls == ["fallback"]
