"""
src/core/schemas.py — typed research data contracts
===================================================

These Pydantic models document and validate the core artifacts used by the
benchmark. They are intentionally close to the existing JSONL fields so legacy
scripts can migrate gradually instead of rewriting every entrypoint at once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictBaseModel(BaseModel):
    """Base model that keeps unknown legacy fields instead of dropping them."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)


class DatasetInstance(StrictBaseModel):
    """One benchmarkable article instance."""

    instance_id: str
    doi: str
    input_source: str = "processed"
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    article_type: str | None = None
    species: list[str] = Field(default_factory=list)
    study_design: str | None = None
    clinical_topic: str | None = None
    text_hash: str | None = None


class SummaryOutput(StrictBaseModel):
    """Normalized summary output from any provider."""

    doi: str
    summarizer: str
    status: Literal["success", "failed", "pending"] = "pending"
    summary: str | None = None
    model_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: str | None = None


class CriteriaScore(StrictBaseModel):
    """One MedHELM rubric criterion returned by the blind judge."""

    score: int = Field(ge=1, le=5)
    reasoning: str = ""


class HallucinationClaim(StrictBaseModel):
    """A quoted unsupported claim and the source evidence used to flag it."""

    claim: str
    source_quote: str
    category: Literal[
        "fabricated_statistics",
        "omitted_caveat",
        "contradiction",
        "unsupported_inference",
    ]
    severity: Literal["minor", "major"]


class AutomaticMetrics(StrictBaseModel):
    """Local secondary metrics that never replace the primary jury score."""

    compression_ratio: float | None = None
    extractive_coverage: float | None = None
    section_coverage: dict[str, Any] = Field(default_factory=dict)
    rouge_1: float | None = None
    rouge_2: float | None = None
    rouge_l: float | None = None


class EvaluationRecord(StrictBaseModel):
    """One scored row in evaluations.jsonl or runs/<run_id>/evaluations.jsonl."""

    benchmark_name: str
    doi: str
    input_source: str = "processed"
    summarizer: str
    judge: str
    judge_model_version: str | None = None
    system_fingerprint: str | None = None
    evaluator_version: str
    rubric_version: str
    criteria_scores: dict[str, CriteriaScore] = Field(default_factory=dict)
    jury_score: float | None = None
    judge_count: int = 1
    valid_judge_count: int = 1
    judge_disagreement: float | None = 0.0
    automatic_metrics: AutomaticMetrics | dict[str, Any] = Field(default_factory=dict)
    strata: dict[str, Any] = Field(default_factory=dict)
    quality_score: int | None = None
    hallucination_count: int = 0
    hallucination_claims: list[HallucinationClaim] = Field(default_factory=list)
    hallucination_categories: list[str] = Field(default_factory=list)
    confidence_score: int = Field(default=1, ge=1, le=5)
    requires_human_review: bool = False
    parse_method: str = "json"
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_response_excerpt: str = ""
    timestamp: str | None = None

    @field_validator("jury_score")
    @classmethod
    def _jury_score_range(cls, value: float | None) -> float | None:
        if value is not None and not 1.0 <= value <= 5.0:
            raise ValueError("jury_score must be between 1.0 and 5.0")
        return value


class FrozenSetManifest(StrictBaseModel):
    """Metadata sidecar for an immutable benchmark set."""

    name: str
    path: str
    sha256: str
    row_count: int
    created_utc: str
    description: str = ""


class RunManifest(StrictBaseModel):
    """Provenance record written under runs/<run_id>/run_manifest.json."""

    run_id: str
    benchmark_name: str
    started_utc: str
    finished_utc: str | None = None
    git_sha: str | None = None
    git_branch: str | None = None
    python_version: str
    platform: str
    dependency_lock_hash: str | None = None
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    config_hashes: dict[str, str] = Field(default_factory=dict)
    dataset_hash: str | None = None
    random_seed: int = 42
    mode: str = "test"
    cli_args: list[str] = Field(default_factory=list)
    selected_instance_ids: list[str] = Field(default_factory=list)
    model_ids: dict[str, str] = Field(default_factory=dict)
    resolved_model_versions: dict[str, str] = Field(default_factory=dict)
    artifact_paths: dict[str, str] = Field(default_factory=dict)


class ProviderHealth(StrictBaseModel):
    """Provider availability check result."""

    provider: str
    ok: bool
    message: str = ""


class ProviderResponse(StrictBaseModel):
    """Normalized provider response payload."""

    provider: str
    raw_text: str
    model_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    system_fingerprint: str | None = None
    cost_usd: float = 0.0


def utc_now_iso() -> str:
    """Return an ISO UTC timestamp for manifests and records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
