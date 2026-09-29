"""Build the configured LLM provider. The only place that knows concrete classes."""

from app.config.settings import Settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.resilience import FallbackProvider, RetryingProvider

SUPPORTED_PROVIDERS = ("ollama", "openai_compat")


def build_provider(name: str, settings: Settings) -> LLMProvider:
    """Build one raw provider by name."""
    match name.lower():
        case "ollama":
            return OllamaProvider(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                timeout_seconds=settings.llm_timeout_seconds,
                context_tokens=settings.llm_context_tokens,
                keep_alive=settings.ollama_keep_alive,
            )
        case "openai_compat":
            if not settings.openai_compat_base_url or not settings.openai_compat_model:
                raise ValueError(
                    "LLM provider 'openai_compat' needs OPENAI_COMPAT_BASE_URL and "
                    "OPENAI_COMPAT_MODEL in .env"
                )
            api_key = settings.openai_compat_api_key
            return OpenAICompatProvider(
                base_url=settings.openai_compat_base_url,
                model=settings.openai_compat_model,
                api_key=api_key.get_secret_value() if api_key else None,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        case other:
            raise ValueError(
                f"Unknown LLM provider '{other}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}"
            )


def create_llm_provider(settings: Settings) -> LLMProvider:
    """Primary provider (+ optional fallback), each wrapped with retries."""
    names = [settings.llm_provider]
    if settings.llm_fallback_provider:
        names.append(settings.llm_fallback_provider)

    providers: list[LLMProvider] = [
        RetryingProvider(build_provider(name, settings), max_retries=settings.llm_max_retries)
        for name in names
    ]
    return providers[0] if len(providers) == 1 else FallbackProvider(providers)
