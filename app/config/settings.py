"""Typed application settings, loaded from environment variables and `.env`.

Every configurable value lives here so the rest of the code never reads
`os.environ` directly. Field names map to env vars case-insensitively,
e.g. `ollama_model` <- `OLLAMA_MODEL`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The folder containing app/, so relative paths work no matter where you start the server.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
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

    # --- Storage ---
    database_path: Path = Path("data/arthur.db")
    vector_store_path: Path = Path("data/memory/chroma")

    # --- Long-term memory ---
    embedding_provider: str = "ollama"
    embedding_model: str = "nomic-embed-text"
    memory_top_k: int = 5  # at most this many memories are added to each prompt
    # Similarity from 0 (unrelated) to 1 (same meaning); below this a memory is ignored.
    # Measured with scripts/calibrate_memory.py for nomic-embed-text:
    # related questions 0.59-0.83, unrelated 0.46-0.51. Re-measure if you change models.
    memory_min_score: float = 0.55

    # --- Agent loop: hard limits so a confused model can never loop forever ---
    agent_max_steps: int = 8
    agent_max_seconds: float = 120.0
    # Planner (Phase 8): multi-part requests are split into steps first.
    agent_planning: bool = True
    agent_plan_max_seconds: float = 240.0

    # --- Tools ---
    # Levels 0..N run without asking; above it the user must confirm. Level 3 is always blocked.
    tools_auto_approve_max_level: int = 1
    tools_blocked: str = ""  # comma-separated tool names to disable, e.g. "weather"
    tools_default_timeout_seconds: float = 10.0

    def resolve(self, path: Path) -> Path:
        """Relative paths in settings are relative to the project folder."""
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def blocked_tools(self) -> set[str]:
        return {name.strip() for name in self.tools_blocked.split(",") if name.strip()}

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
