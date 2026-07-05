"""Anthropic adapter boundary."""

from adapters.base import ProviderAdapter


class AnthropicAdapter(ProviderAdapter):
    provider_name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"
    default_model = "claude-sonnet-4-5-20250929"
