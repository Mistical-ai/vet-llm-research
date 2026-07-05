"""OpenAI adapter boundary."""

from adapters.base import ProviderAdapter


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-5.4"
