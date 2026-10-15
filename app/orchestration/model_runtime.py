from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from agents import OpenAIChatCompletionsModel, Runner, set_tracing_disabled
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
    """Run Agents SDK calls against OpenAI with a configurable compatible fallback.

    The fallback endpoint must implement an OpenAI-compatible Chat Completions API
    sufficiently for Agents SDK structured outputs. Provider secrets never leave
    server-side configuration.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._fallback_model: OpenAIChatCompletionsModel | None = None
        self._primary_unavailable_until = 0.0

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

        # Agents SDK tracing defaults to OpenAI. In fallback-only deployments,
        # disable tracing so no OpenAI credential is required just to export spans.
        if not settings.openai_api_key and self._fallback_model is not None:
            set_tracing_disabled(disabled=True)

    @property
    def fallback_configured(self) -> bool:
        return self._fallback_model is not None

    @property
    def fallback_model(self) -> OpenAIChatCompletionsModel:
        if self._fallback_model is None:
            raise RuntimeError("Custom LLM fallback is not configured")
        return self._fallback_model

    def _primary_circuit_open(self) -> bool:
        return time.monotonic() < self._primary_unavailable_until

    def _open_primary_circuit(self) -> None:
        self._primary_unavailable_until = time.monotonic() + self.settings.custom_llm_failover_cooldown_seconds

    async def run(
        self,
        build_agent: Callable[[Any | None], Any],
        input_text: str,
    ):
        """Run primary first, then fail over on provider-unavailability failures."""
        if not self.settings.openai_api_key:
            if self._fallback_model is None:
                raise RuntimeError("No model runtime is configured")
            return await Runner.run(build_agent(self._fallback_model), input_text)

        if self._fallback_model is not None and self._primary_circuit_open():
            return await Runner.run(build_agent(self._fallback_model), input_text)

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
            return await Runner.run(build_agent(self._fallback_model), input_text)
