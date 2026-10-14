from functools import lru_cache

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
