"""Adapter registry."""

from __future__ import annotations

from adapters.anthropic_adapter import AnthropicAdapter
from adapters.base import ProviderAdapter
from adapters.gemini_adapter import GeminiAdapter
from adapters.openai_adapter import OpenAIAdapter

ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
}


def get_adapter(provider: str, *, model_id: str | None = None) -> ProviderAdapter:
    """Return a provider adapter by key."""
    key = provider.lower().strip()
    if key not in ADAPTERS:
        raise KeyError(f"Unknown provider adapter: {provider}")
    return ADAPTERS[key](model_id=model_id)


def healthcheck_all() -> dict[str, dict[str, object]]:
    """Return healthcheck payloads for all known adapters."""
    return {name: adapter().healthcheck().model_dump() for name, adapter in ADAPTERS.items()}
