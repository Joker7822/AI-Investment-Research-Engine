from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from agents import OpenAIChatCompletionsModel, RunConfig, Runner, set_tracing_disabled
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from app.core.settings import Settings


logger = logging.getLogger(__name__)

ModelProviderMode = Literal["auto", "openai", "custom"]
_model_provider_override: ContextVar[ModelProviderMode] = ContextVar(
    "model_provider_override",
    default="auto",
)


class ModelProviderUnavailableError(RuntimeError):
    """Raised when a caller explicitly selects an unconfigured model provider."""

    def __init__(self, provider: ModelProviderMode):
        super().__init__(f"AI provider is unavailable: {provider}")
        self.provider = provider


@contextmanager
def use_model_provider(provider: ModelProviderMode) -> Iterator[None]:
    """Apply one provider selection to the current async request context."""
    token = _model_provider_override.set(provider)
    try:
        yield
    finally:
        _model_provider_override.reset(token)


def custom_llm_configured(settings: Settings) -> bool:
    values = (
        settings.custom_llm_base_url,
        settings.custom_llm_api_key,
        settings.custom_llm_model,
    )
    populated = [bool(value and value.strip()) for value in values]
    if any(populated) and not all(populated):
        raise ValueError(
            "CUSTOM_LLM_BASE_URL, CUSTOM_LLM_API_KEY, and CUSTOM_LLM_MODEL must be configured together"
        )
    return all(populated)


def model_runtime_configured(settings: Settings) -> bool:
    return bool(settings.openai_api_key) or custom_llm_configured(settings)


def provider_mode_configured(settings: Settings, provider: ModelProviderMode) -> bool:
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "custom":
        return custom_llm_configured(settings)
    return model_runtime_configured(settings)


def primary_provider_unavailable(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            AuthenticationError,
            PermissionDeniedError,
        ),
    ):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or exc.status_code >= 500
    return False


class ModelRuntime:
    """Run Agents SDK calls against selectable primary/custom model providers.

    `auto` prefers OpenAI and fails over to the configured OpenAI-compatible custom
    Chat Completions endpoint when the primary provider is unavailable. `openai`
    and `custom` force one provider and never silently switch to the other.
    Provider secrets remain server-side configuration only.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._fallback_model: OpenAIChatCompletionsModel | None = None
        self._primary_unavailable_until = 0.0
        self._custom_run_config = RunConfig(tracing_disabled=True)

        if custom_llm_configured(settings):
            client = AsyncOpenAI(
                api_key=str(settings.custom_llm_api_key),
                base_url=str(settings.custom_llm_base_url),
                timeout=settings.custom_llm_timeout_seconds,
            )
            self._fallback_model = OpenAIChatCompletionsModel(
                model=str(settings.custom_llm_model),
                openai_client=client,
            )

        # Agents SDK tracing defaults to OpenAI. In custom-only deployments,
        # disable tracing globally so no OpenAI credential is required for spans.
        if not settings.openai_api_key and self._fallback_model is not None:
            set_tracing_disabled(disabled=True)

    @property
    def fallback_configured(self) -> bool:
        return self._fallback_model is not None

    @property
    def fallback_model(self) -> OpenAIChatCompletionsModel:
        if self._fallback_model is None:
            raise ModelProviderUnavailableError("custom")
        return self._fallback_model

    def supports(self, provider: ModelProviderMode) -> bool:
        if provider == "openai":
            return bool(self.settings.openai_api_key)
        if provider == "custom":
            return self._fallback_model is not None
        return bool(self.settings.openai_api_key) or self._fallback_model is not None

    def _primary_circuit_open(self) -> bool:
        return time.monotonic() < self._primary_unavailable_until

    def _open_primary_circuit(self) -> None:
        self._primary_unavailable_until = time.monotonic() + self.settings.custom_llm_failover_cooldown_seconds

    async def _run_custom(self, build_agent: Callable[[Any | None], Any], input_text: str):
        if self._fallback_model is None:
            raise ModelProviderUnavailableError("custom")
        return await Runner.run(
            build_agent(self._fallback_model),
            input_text,
            run_config=self._custom_run_config,
        )

    async def run(
        self,
        build_agent: Callable[[Any | None], Any],
        input_text: str,
        *,
        provider: ModelProviderMode = "auto",
    ):
        """Run one agent using the requested provider selection mode."""
        request_override = _model_provider_override.get()
        effective_provider: ModelProviderMode = (
            request_override if provider == "auto" and request_override != "auto" else provider
        )

        if effective_provider == "openai":
            if not self.settings.openai_api_key:
                raise ModelProviderUnavailableError("openai")
            return await Runner.run(build_agent(None), input_text)

        if effective_provider == "custom":
            return await self._run_custom(build_agent, input_text)

        if not self.settings.openai_api_key:
            return await self._run_custom(build_agent, input_text)

        if self._fallback_model is not None and self._primary_circuit_open():
            return await self._run_custom(build_agent, input_text)

        try:
            return await Runner.run(build_agent(None), input_text)
        except Exception as exc:
            if self._fallback_model is None or not primary_provider_unavailable(exc):
                raise
            self._open_primary_circuit()
            logger.warning(
                "Primary model provider unavailable; switching to configured custom fallback: reason=%s",
                type(exc).__name__,
            )
            return await self._run_custom(build_agent, input_text)
