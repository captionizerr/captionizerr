"""LLM providers. Anthropic is the reference adapter; others implement the same Provider protocol."""
from .base import Provider, ProviderResult, ProviderError, RefusalError

__all__ = ["Provider", "ProviderResult", "ProviderError", "RefusalError"]


def make_provider(s):
    """The configured provider for a Settings object."""
    from ..pricing import register
    if s.provider == "ollama":
        from .ollama import OllamaProvider
        register(s.ollama_model, 0.0, 0.0)
        return OllamaProvider(s.ollama_url, s.ollama_model, s.ollama_num_ctx, s.ollama_max_output)
    if s.provider == "openai":
        from .openai_compat import OpenAICompatProvider
        register(s.openai_model, s.openai_price_in, s.openai_price_out)
        return OpenAICompatProvider(s.openai_base_url, s.openai_model, s.openai_key, s.openai_max_output)
    from .anthropic_provider import AnthropicProvider
    return AnthropicProvider(model=s.model, effort=s.effort)
