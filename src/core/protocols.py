"""
src/core/protocols.py — adapter and metric interfaces
=====================================================

Protocols define the narrow contracts orchestration code should depend on.
Provider SDKs, metrics, and reports can then evolve independently as long as
they keep returning the normalized payloads described here.
"""

from __future__ import annotations

from typing import Any, Protocol

from core.schemas import DatasetInstance, ProviderHealth, ProviderResponse


class LLMProvider(Protocol):
    """Provider adapter contract for model calls."""

    provider_name: str

    def healthcheck(self) -> ProviderHealth:
        """Return whether the provider adapter is configured enough to run."""

    def summarize(self, instance: DatasetInstance, spec: dict[str, Any]) -> ProviderResponse:
        """Summarize one dataset instance and return a normalized response."""

    def judge(self, reference_text: str, candidate_summary: str, spec: dict[str, Any]) -> ProviderResponse:
        """Judge one candidate summary and return a normalized response."""


class Metric(Protocol):
    """Metric contract for deterministic and stochastic report metrics."""

    name: str
    deterministic: bool

    def evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Evaluate one payload and return a JSON-serializable result."""
