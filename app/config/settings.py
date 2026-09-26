"""Typed application settings, loaded from environment variables and `.env`.

Every configurable value lives here so the rest of the code never reads
`os.environ` directly. Field names map to env vars case-insensitively,
e.g. `ollama_model` <- `OLLAMA_MODEL`.
"""

from functools import lru_cache
from typing import Literal

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
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    llm_timeout_seconds: float = 120.0

    @property
    def json_logs(self) -> bool:
        """Machine-readable JSON logs in production, pretty logs while developing."""
        return self.arthur_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return one shared Settings instance (read `.env` only once)."""
    return Settings()
