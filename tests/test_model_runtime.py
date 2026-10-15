from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, RateLimitError

from app.core.settings import Settings
from app.orchestration import model_runtime as runtime_module
from app.orchestration.model_runtime import (
    ModelProviderUnavailableError,
    ModelRuntime,
    custom_llm_configured,
    current_model_provider,
    model_runtime_configured,
    primary_provider_unavailable,
    use_model_provider,
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


def test_request_scoped_provider_override_is_restored():
    assert current_model_provider() == "auto"
    with use_model_provider("custom"):
        assert current_model_provider() == "custom"
        assert current_model_provider("openai") == "openai"
    assert current_model_provider() == "auto"


@pytest.mark.asyncio
async def test_runtime_fails_over_after_primary_rate_limit_and_opens_circuit(monkeypatch):
    settings = _settings()
    runtime = ModelRuntime(settings)
    calls: list[tuple[object, object | None]] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append((agent, kwargs.get("run_config")))
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
    assert [item[0] for item in calls] == ["primary", "fallback", "fallback"]
    assert calls[1][1].tracing_disabled is True
    assert calls[2][1].tracing_disabled is True


@pytest.mark.asyncio
async def test_runtime_keeps_primary_when_primary_succeeds(monkeypatch):
    runtime = ModelRuntime(_settings())
    calls: list[object] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append(agent)
        return SimpleNamespace(final_output="primary-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    result = await runtime.run(build_agent, "payload")
    assert result.final_output == "primary-ok"
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_forced_openai_never_falls_back(monkeypatch):
    runtime = ModelRuntime(_settings())
    calls: list[object] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append(agent)
        raise _rate_limit_error()

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    with pytest.raises(RateLimitError):
        await runtime.run(build_agent, "payload", provider="openai")
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_forced_custom_never_calls_primary_and_disables_tracing(monkeypatch):
    runtime = ModelRuntime(_settings())
    calls: list[tuple[object, object | None]] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append((agent, kwargs.get("run_config")))
        return SimpleNamespace(final_output="custom-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    result = await runtime.run(build_agent, "payload", provider="custom")
    assert result.final_output == "custom-ok"
    assert calls[0][0] == "fallback"
    assert calls[0][1].tracing_disabled is True


@pytest.mark.asyncio
async def test_request_scoped_custom_override_controls_auto_mode(monkeypatch):
    runtime = ModelRuntime(_settings())
    calls: list[object] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append(agent)
        return SimpleNamespace(final_output="custom-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    with use_model_provider("custom"):
        result = await runtime.run(build_agent, "payload")
    assert result.final_output == "custom-ok"
    assert calls == ["fallback"]


@pytest.mark.asyncio
async def test_fallback_only_runtime_never_calls_primary(monkeypatch):
    monkeypatch.setattr(runtime_module, "set_tracing_disabled", lambda **kwargs: None)
    runtime = ModelRuntime(_settings(openai_api_key=None))
    calls: list[object] = []

    async def fake_run(agent, input_text, **kwargs):
        calls.append(agent)
        return SimpleNamespace(final_output="fallback-ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    result = await runtime.run(build_agent, "payload")
    assert result.final_output == "fallback-ok"
    assert calls == ["fallback"]


@pytest.mark.asyncio
async def test_forced_unconfigured_provider_fails_explicitly(monkeypatch):
    monkeypatch.setattr(runtime_module, "set_tracing_disabled", lambda **kwargs: None)
    runtime = ModelRuntime(_settings(openai_api_key=None))

    def build_agent(model_override):
        return "primary" if model_override is None else "fallback"

    with pytest.raises(ModelProviderUnavailableError) as exc_info:
        await runtime.run(build_agent, "payload", provider="openai")
    assert exc_info.value.provider == "openai"
