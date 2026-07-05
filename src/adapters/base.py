"""Base provider adapter and response normalization helpers."""

from __future__ import annotations

import os
from typing import Any

from core.schemas import DatasetInstance, ProviderHealth, ProviderResponse


class ProviderAdapter:
    """Small base adapter used before live SDK calls are fully migrated."""

    provider_name: str = "unknown"
    env_key: str = ""
    default_model: str = ""

    def __init__(self, model_id: str | None = None) -> None:
        self.model_id = model_id or os.getenv(f"{self.provider_name.upper()}_MODEL", self.default_model)

    def healthcheck(self) -> ProviderHealth:
        """Check configuration without making a network request."""
        if not self.env_key:
            return ProviderHealth(provider=self.provider_name, ok=True, message="No API key required.")
        if os.getenv("DRY_RUN", "true").lower() == "true":
            return ProviderHealth(provider=self.provider_name, ok=True, message="DRY_RUN=true; network disabled.")
        ok = bool(os.getenv(self.env_key))
        message = "API key present." if ok else f"Missing {self.env_key}."
        return ProviderHealth(provider=self.provider_name, ok=ok, message=message)

    def summarize(self, instance: DatasetInstance, spec: dict[str, Any]) -> ProviderResponse:
        """Return a deterministic dry-run response or fail before live migration."""
        if spec.get("dry_run", True):
            return normalize_response(
                provider=self.provider_name,
                raw_text=f"[DRY_RUN] Summary placeholder for {instance.doi}",
                model_version=self.model_id,
            )
        raise NotImplementedError("Live summarize calls are still handled by legacy summarizer.py.")

    def judge(self, reference_text: str, candidate_summary: str, spec: dict[str, Any]) -> ProviderResponse:
        """Return a deterministic dry-run judge response or fail before live migration."""
        if spec.get("dry_run", True):
            return normalize_response(
                provider=self.provider_name,
                raw_text="{\"criteria_scores\": {}, \"reasoning\": \"DRY_RUN adapter response\"}",
                model_version=self.model_id,
            )
        raise NotImplementedError("Live judge calls are still handled by legacy evaluator.py.")


def normalize_response(
    *,
    provider: str,
    raw_text: str,
    model_version: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    system_fingerprint: str | None = None,
    cost_usd: float = 0.0,
) -> ProviderResponse:
    """Convert provider-specific details into the shared response schema."""
    return ProviderResponse(
        provider=provider,
        raw_text=raw_text,
        model_version=model_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        system_fingerprint=system_fingerprint,
        cost_usd=cost_usd,
    )
