"""Gemini adapter boundary."""

from adapters.base import ProviderAdapter


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"
    env_key = "GEMINI_API_KEY"
    default_model = "gemini-3-pro"
