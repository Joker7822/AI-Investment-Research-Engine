from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str | None = None
    openai_agent_model: str | None = None
    max_parallel_agents: int = 8
    agent_config_path: str = "config/agents.yaml"

    sec_user_agent: str | None = None
    sec_timeout_seconds: float = 15.0

    massive_api_key: str | None = None
    massive_timeout_seconds: float = 15.0
    massive_lookback_days: int = 400

    database_url: str | None = None
    database_schema_path: str = "db/schema.sql"

    edinet_api_key: str | None = None
    edinet_timeout_seconds: float = 30.0
    edinet_index_limit: int = 8

    api_name: str = "Investment Research API"
    api_version: str = "1.0.0"
    # Backward-compatible comma-separated SHA-256 hashes of valid API keys.
    # Each legacy hash is deterministically mapped to its own client_id.
    public_api_key_hashes: str | None = None
    # Optional explicit multi-tenant mapping: comma-separated client_id=sha256 entries.
    # Example: acme=0123...cdef,internal=abcd...7890
    # This allows API-key rotation while retaining a stable client_id by assigning
    # multiple key hashes to the same client_id.
    public_api_client_keys: str | None = None
    api_docs_enabled: bool = True

    # Per-client research admission controls. A work unit is one selected agent
    # execution. Zero disables a window quota; the per-request agent cap stays on.
    api_research_rate_limit_per_minute: int = Field(default=3, ge=0)
    api_research_daily_request_quota: int = Field(default=24, ge=0)
    api_research_daily_agent_quota: int = Field(default=600, ge=0)
    api_research_max_agents_per_request: int = Field(default=36, ge=1)

    # When enabled, startup fails unless DB connectivity, OpenAI configuration,
    # and public API authentication are all production-ready.
    strict_startup_readiness: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
