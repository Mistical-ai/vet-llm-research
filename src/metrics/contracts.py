"""Metric input/output contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetricInput(BaseModel):
    """Generic payload passed into a metric implementation."""

    model_config = ConfigDict(extra="allow")
    reference_text: str = ""
    candidate_summary: str = ""
    evaluation_record: dict[str, Any] = Field(default_factory=dict)


class MetricResult(BaseModel):
    """JSON-serializable metric output."""

    name: str
    value: float | int | bool | str | None
    deterministic: bool = True
    details: dict[str, Any] = Field(default_factory=dict)
