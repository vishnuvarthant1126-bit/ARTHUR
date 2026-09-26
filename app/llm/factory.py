"""Build the configured LLM provider. The only place that knows concrete classes."""

from app.config.settings import Settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider

SUPPORTED_PROVIDERS = ("ollama",)


def create_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider.lower():
        case "ollama":
            return OllamaProvider(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        case other:
            raise ValueError(
                f"Unknown LLM_PROVIDER '{other}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}"
            )
