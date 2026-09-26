"""Typed application settings, loaded from environment variables and `.env`.

Every configurable value lives here so the rest of the code never reads
`os.environ` directly. Field names map to env vars case-insensitively,
e.g. `ollama_model` <- `OLLAMA_MODEL`.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env holds settings for later phases too
    )

    # --- App ---
    arthur_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- LLM ---
    llm_provider: str = "ollama"
    llm_fallback_provider: str | None = None
    llm_max_retries: int = 2
    llm_timeout_seconds: float = 120.0
    # Context window: how many tokens the model can read at once (prompt + history + reply).
    llm_context_tokens: int = 8192
    # Part of the window kept free for the reply itself.
    llm_reply_reserve_tokens: int = 1024

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"

    openai_compat_base_url: str | None = None
    # SecretStr hides the value if settings are ever printed or logged.
    openai_compat_api_key: SecretStr | None = None
    openai_compat_model: str | None = None

    # --- Short-term memory ---
    memory_max_history_messages: int = 40
    memory_max_sessions: int = 100
    memory_session_ttl_minutes: int = 240

    @field_validator(
        "llm_fallback_provider", "openai_compat_base_url", "openai_compat_model", mode="before"
    )
    @classmethod
    def empty_is_none(cls, value: object) -> object:
        """`KEY=` (blank) in .env means "not set"."""
        return value or None

    @field_validator("openai_compat_api_key", mode="before")
    @classmethod
    def empty_secret_is_none(cls, value: object) -> object:
        return value or None

    @property
    def json_logs(self) -> bool:
        """Machine-readable JSON logs in production, pretty logs while developing."""
        return self.arthur_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return one shared Settings instance (read `.env` only once)."""
    return Settings()
