"""
llm-sum/summarizer.py — Multi-model summarisation router
==========================================================

This module is the heart of Phase 3's "fair comparison" guarantee. Every
provider (OpenAI, Anthropic, Gemini) is called with:
    - the same prompt (loaded from PROMPT_FILE),
    - the same temperature (0.0) and seed (42) where supported,
    - the same max output tokens.

Every successful call returns the SAME dict shape so downstream code never
branches on provider. Manifest-driven runs persist to ``data/summaries.jsonl``;
folder-driven ``summarize-all`` runs persist one text file per source article
under ``data/summaries_pdf/`` and ``data/summaries_txt/`` so the raw-PDF and
processed-text comparisons stay separate.

    {
        "status": "success",
        "summary": "<text>",
        "input_tokens": int,
        "output_tokens": int,
        "model_version": "<exact-id-from-provider>",
        "timestamp": "<UTC ISO-8601>",
    }

DESIGN DECISIONS
----------------
* Run modes are controlled by ``PHASE3_MODE`` in ``.env`` (test, single,
  dev, batch). See ``phase3_mode.py``. The summariser does not hardcode
  any safety constants any more — the safe default is encoded in the
  fact that ``test`` is the resolved mode when ``PHASE3_MODE`` is unset.

* Model versions are recorded EXACTLY as the provider returns them
  (e.g. "gpt-5.4-0325-preview", not "gpt-5"). Providers silently update
  models behind a stable alias — logging the exact string is how we detect
  performance drift across reruns.

* Token counts come from `usage.*` in the provider response. Estimating
  after the fact accumulates ±5% per call; over 1000 calls that mismatch
  is large enough to break a tight research budget.

* The provider router is a dispatch dict, not an if/elif chain. Adding a
  fourth provider in the future is a one-line change.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import base64
import copy
import importlib
import json
import os
import sys
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from models_config import all_providers, compute_cost, get_model_spec  # noqa: E402
from file_paths import descriptive_stem, doi_to_slug, resolve_existing_pdf_path  # noqa: E402
from utils import BudgetGuard, log_error, require_positive_budget_for_real_run, sleep_for_model  # noqa: E402
from extract import truncate_to_limit  # noqa: E402
from phase3_mode import ModeProfile, resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
RAW_DIR = DATA_DIR / "raw"
SUMMARIES_PDF_DIR = DATA_DIR / "summaries_pdf"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
# The structured VeterinarySummary schema needs enough room for valid JSON plus
# readable prose. 500 tokens caused provider-side truncation and unparsable
# OpenAI responses, so the default leaves headroom for complete summaries.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1200"))
GEMINI_MAX_OUTPUT_TOKENS = int(
    os.getenv("GEMINI_MAX_OUTPUT_TOKENS", str(max(MAX_OUTPUT_TOKENS, 2400)))
)
PROMPT_FILE = Path(os.getenv("PROMPT_FILE", "llm-sum/prompts/summarization_v1.txt"))
PROMPT_MODE = os.getenv("PROMPT_MODE", "shared")
VALID_PROMPT_MODES = ("shared", "provider_specific")
PROVIDER_PROMPT_FILES = {
    "openai": Path(os.getenv("OPENAI_PROMPT_FILE", "llm-sum/prompts/summarization_openai_v1.txt")),
    "anthropic": Path(os.getenv("ANTHROPIC_PROMPT_FILE", "llm-sum/prompts/summarization_anthropic_v1.txt")),
    "gemini": Path(os.getenv("GEMINI_PROMPT_FILE", "llm-sum/prompts/summarization_gemini_v1.txt")),
}
GUIDE_SUMMARY_FILE = Path(
    os.getenv("GUIDE_SUMMARY_FILE", "llm-sum/prompts/guide_summary_template.txt")
)

# Maximum characters of article text sent to the LLM. 0 = no limit (use
# full cached text). Set via MAX_INPUT_CHARS in .env to tune cost vs coverage.
# 40 000 chars ≈ 10 000 tokens ≈ a full 15-page research article.
MAX_INPUT_CHARS: int = int(os.getenv("MAX_INPUT_CHARS", "0"))
VALID_INPUT_SOURCES = ("processed", "raw_text", "pdf")

# OpenAI rate-limit retry config (separate from the general outer retry in
# generate_summary — these control the inner 429-specific backoff layer).
OPENAI_MAX_RETRIES: int = int(os.getenv("OPENAI_MAX_RETRIES", "6"))
OPENAI_RETRY_BASE_DELAY: float = float(os.getenv("OPENAI_RETRY_BASE_DELAY", "1.0"))
OPENAI_RETRY_MAX_DELAY: float = float(os.getenv("OPENAI_RETRY_MAX_DELAY", "60.0"))


# ---------------------------------------------------------------------------
# Structured summary schema
# ---------------------------------------------------------------------------

class VeterinarySummary(BaseModel):
    """
    One provider-independent schema for every Phase 3 clinical summary.

    The native structured-output APIs use this model as their contract. The
    local code also validates every provider response against it before writing
    to data/summaries.jsonl, so downstream analysis receives a predictable
    database-shaped object instead of provider-specific prose.
    """
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(
        description=(
            "One concise sentence stating the most clinically important takeaway "
            "from the article. Use only information supported by the article text."
        ),
    )
    objective: str = Field(
        description=(
            "The article's research question, objective, or hypothesis. Write "
            "'Not reported' if the article does not state one clearly."
        ),
    )
    study_design: str = Field(
        description=(
            "The study design reported by the article, such as retrospective "
            "cohort, randomized trial, cross-sectional survey, case-control, "
            "experimental study, or 'Not reported'."
        ),
    )
    species: str = Field(
        description=(
            "Animal species or population studied, for example dogs, cats, "
            "horses, cattle, mixed species, or 'Not reported'."
        ),
    )
    sample_size: int | None = Field(
        default=None,
        description=(
            "Number of animals, samples, records, owners, or cases analyzed. "
            "Use null if no sample size is reported. Do not guess."
        ),
    )
    key_methods: list[str] = Field(
        description=(
            "Main methods, interventions, exposures, measurements, comparisons, "
            "or statistical approach. Each item must be directly supported by "
            "the article text."
        ),
    )
    key_findings: list[str] = Field(
        description=(
            "Most important findings, prioritizing quantitative results and "
            "clinically meaningful outcomes. Include exact numbers only when "
            "they appear in the source text."
        ),
    )
    clinical_significance: str = Field(
        description=(
            "How a practicing veterinarian should interpret or apply the "
            "findings. Avoid advice that goes beyond the article."
        ),
    )
    limitations: list[str] = Field(
        description=(
            "Important limitations, caveats, population constraints, or sources "
            "of uncertainty stated or directly implied by the article."
        ),
    )
    summary_text: str = Field(
        description=(
            "A readable clinical prose summary under 400 words. It must include "
            "Objective, Key Methods, Primary Results, and Clinical Significance, "
            "and must not include unsupported facts."
        ),
    )


def veterinary_summary_to_result(parsed: VeterinarySummary) -> dict:
    """
    Convert a validated Pydantic summary into the repo's provider result shape.

    Beginners' guide:
      - parsed.model_dump() turns the Pydantic object into a normal dict.
      - parsed.model_dump_json() turns it into a perfect JSON string for JSONL.
      - The plain-prose summary remains available at parsed.summary_text so the
        existing blind evaluator can keep judging readable summaries.
    """
    parsed = VeterinarySummary.model_validate(parsed)
    return {
        "summary": parsed.summary_text,
        "structured_summary": parsed.model_dump(),
    }


def _as_list(value: Any) -> list[str]:
    """
    Normalize provider output into a list of strings.

    Some providers return a single paragraph where our schema expects a list.
    Instead of throwing away an otherwise useful summary, this helper wraps that
    text as one list item. Missing values become an empty list.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _summary_text_from_payload(payload: dict[str, Any]) -> str:
    """
    Build readable prose when a provider omits the summary_text field.

    This is a repair path for live model responses that mostly followed the
    schema but left out one required field. The facts still come only from the
    provider's extracted fields; we just assemble them into the prose field that
    downstream evaluation expects.
    """
    parts = []
    for label, key in (
        ("Objective", "objective"),
        ("Key Methods", "key_methods"),
        ("Primary Results", "key_findings"),
        ("Clinical Significance", "clinical_significance"),
    ):
        value = payload.get(key)
        if isinstance(value, list):
            value = " ".join(str(item).strip() for item in value if str(item).strip())
        value = str(value or "").strip()
        if value and value != "Not reported":
            parts.append(f"{label}: {value}")
    return " ".join(parts) or str(payload.get("headline") or "Not reported")


def coerce_veterinary_summary(payload: Any) -> VeterinarySummary:
    """
    Validate provider output, filling safe placeholders for omitted fields.

    The LLM APIs sometimes return a partial tool/schema object even when the
    request says every field is required. This function keeps a real run from
    failing just because one provider omitted, for example, ``limitations``.
    Missing facts become "Not reported" or empty lists; we never invent numbers
    or conclusions.
    """
    if isinstance(payload, VeterinarySummary):
        return payload
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise ValueError("VeterinarySummary payload was not a dict-like object.")

    repaired = dict(payload)
    repaired["headline"] = str(repaired.get("headline") or "Not reported")
    repaired["objective"] = str(repaired.get("objective") or "Not reported")
    repaired["study_design"] = str(repaired.get("study_design") or "Not reported")
    repaired["species"] = str(repaired.get("species") or "Not reported")
    repaired["sample_size"] = repaired.get("sample_size")
    repaired["key_methods"] = _as_list(repaired.get("key_methods"))
    repaired["key_findings"] = _as_list(repaired.get("key_findings"))
    repaired["clinical_significance"] = str(
        repaired.get("clinical_significance") or "Not reported"
    )
    repaired["limitations"] = _as_list(repaired.get("limitations"))
    repaired["summary_text"] = str(
        repaired.get("summary_text") or _summary_text_from_payload(repaired)
    )
    return VeterinarySummary.model_validate(repaired)


def append_veterinary_summary_jsonl_example(parsed: VeterinarySummary, path: Path) -> None:
    """
    Educational example: append one schema-perfect summary row to a JSONL file.

    The production pipeline writes richer rows that include DOI, model name,
    token counts, and timestamps. This helper intentionally shows only the core
    Pydantic pattern for learning purposes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(parsed.model_dump_json() + "\n")


def _successful_summary_result(
    *,
    parsed: VeterinarySummary,
    input_tokens: int,
    output_tokens: int,
    model_version: str,
    system_fingerprint: str | None = None,
) -> dict:
    """Wrap a validated summary with provider metadata used downstream."""
    result = veterinary_summary_to_result(parsed)
    result.update({
        "status": "success",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_version": model_version,
        "system_fingerprint": system_fingerprint,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return result


def _is_dry_run() -> bool:
    """
    Read DRY_RUN at call time so tests (and the user toggling .env between
    runs) get the current value. ``PHASE3_MODE=test`` ALSO forces dry-run
    via ``resolve_mode().dry_run``, so test mode never reaches a real API
    even if ``DRY_RUN=false`` was set.
    """
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return True
    # PHASE3_MODE=test is the last guardrail: even with DRY_RUN=false,
    # test mode short-circuits to mocks.
    return resolve_mode().dry_run


# Backwards-compatible module attribute for older tests/importers. Runtime code
# must call `_is_dry_run()` directly so a stale shell value cannot keep a paid
# single-mode run on mocks after `.env` or CLI mode has changed.
DRY_RUN = _is_dry_run()


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_prompt(prompt_file: Path = PROMPT_FILE) -> str:
    """
    Load the summarisation prompt template. Must contain '{ARTICLE_TEXT}'.
    """
    path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    template = path.read_text(encoding="utf-8")
    if "{ARTICLE_TEXT}" not in template:
        raise ValueError(
            f"Prompt template {path} must contain the '{{ARTICLE_TEXT}}' placeholder."
        )
    return template


def _resolve_repo_path(path: Path) -> Path:
    """Resolve relative config paths from the repository root, not the shell cwd."""
    return path if path.is_absolute() else REPO_ROOT / path


def _prompt_mode() -> str:
    """
    Return the active prompt mode from the current environment.

    This is read at call time, not import time, so tests and one-off shell runs
    can change PROMPT_MODE without needing a fresh Python process. Invalid modes
    fail during prompt validation before any paid API confirmation.
    """
    mode = os.getenv("PROMPT_MODE", PROMPT_MODE).strip().lower()
    if mode not in VALID_PROMPT_MODES:
        raise ValueError(
            f"PROMPT_MODE must be one of {', '.join(VALID_PROMPT_MODES)}; got {mode!r}."
        )
    return mode


def _provider_prompt_file(provider: str) -> Path:
    """
    Resolve the configured prompt path for one provider.

    The provider-specific env vars are read lazily for the same reason as
    PROMPT_MODE: a test or short run can change one variable without reloading
    this module.
    """
    env_names = {
        "openai": "OPENAI_PROMPT_FILE",
        "anthropic": "ANTHROPIC_PROMPT_FILE",
        "gemini": "GEMINI_PROMPT_FILE",
    }
    if provider not in env_names:
        raise KeyError(f"Unknown provider '{provider}'")
    return Path(os.getenv(env_names[provider], str(PROVIDER_PROMPT_FILES[provider])))


def load_optional_guide_summary(guide_file: Path | None = GUIDE_SUMMARY_FILE) -> str | None:
    """
    Load the user's human-written format guide, if one exists.

    This is intentionally optional. If the file is missing or empty, the pipeline
    behaves exactly like before. That lets the user add a guide for prompt
    experiments without changing the production summarisation path by accident.
    """
    if guide_file is None:
        return None

    path = _resolve_repo_path(guide_file)
    if not path.exists():
        return None

    guide = path.read_text(encoding="utf-8").strip()
    return guide or None


def apply_guide_summary_to_prompt(template: str, guide_summary: str | None) -> str:
    """
    Add a format-only guide summary to the prompt without making it source data.

    The guide is wrapped in a warning block that tells the LLM to copy only the
    section order, tone, and level of detail. The important research-safety rule:
    no species, diseases, numbers, treatments, or conclusions from the guide may
    appear in a new summary unless they are also in the target article.
    """
    if not guide_summary:
        return template

    guide_block = (
        "Format guide summary (structure only, not source evidence):\n"
        "The following human-written summary is ONLY a formatting example. Use it "
        "for section names, section order, tone, and level of detail. Do NOT copy "
        "its species, diseases, treatments, numbers, outcomes, conclusions, or "
        "any other factual claims. Every fact in your output must come from the "
        "target article/PDF, not from this guide.\n"
        "<FORMAT_GUIDE_SUMMARY>\n"
        f"{guide_summary}\n"
        "</FORMAT_GUIDE_SUMMARY>"
    )

    article_marker = "Article text:\n{ARTICLE_TEXT}"
    if article_marker in template:
        return template.replace(
            article_marker,
            f"{guide_block}\n\n{article_marker}",
            1,
        )

    # Fallback for future prompt templates: keep the guide before the article
    # content so the target paper remains the final source material in the prompt.
    return template.replace(
        "{ARTICLE_TEXT}",
        f"{guide_block}\n\nTarget article:\n{{ARTICLE_TEXT}}",
        1,
    )


def load_prompt_with_optional_guide(
    guide_summary_path: Path | None = GUIDE_SUMMARY_FILE,
) -> tuple[str, str | None, Path]:
    """
    Load the final prompt template that will be sent to providers.

    This small wrapper lets the CLI validate prompt files before asking the user
    to confirm a paid run. If the required ``{ARTICLE_TEXT}`` placeholder is
    missing, the script now fails early instead of waiting until after the user
    types ``yes``.
    """
    resolved_guide_path = _resolve_repo_path(guide_summary_path or GUIDE_SUMMARY_FILE)
    guide_summary = load_optional_guide_summary(guide_summary_path or GUIDE_SUMMARY_FILE)
    prompt_template = apply_guide_summary_to_prompt(load_prompt(), guide_summary)
    return prompt_template, guide_summary, resolved_guide_path


def load_provider_prompt_templates_with_optional_guide(
    providers: list[str] | None = None,
    guide_summary_path: Path | None = GUIDE_SUMMARY_FILE,
) -> tuple[dict[str, str], str | None, Path, dict[str, Path]]:
    """
    Load the prompt template each provider should receive for this run.

    ``PROMPT_MODE=shared`` intentionally maps every provider to the same prompt
    to preserve the fair-comparison baseline. ``provider_specific`` loads one
    file per provider for prompt-engineering experiments while keeping the same
    validation and optional guide-summary wrapper.
    """
    provider_list = providers or list(all_providers())
    resolved_guide_path = _resolve_repo_path(guide_summary_path or GUIDE_SUMMARY_FILE)
    guide_summary = load_optional_guide_summary(guide_summary_path or GUIDE_SUMMARY_FILE)
    mode = _prompt_mode()

    if mode == "shared":
        shared_template = apply_guide_summary_to_prompt(load_prompt(), guide_summary)
        shared_path = _resolve_repo_path(PROMPT_FILE)
        return (
            {provider: shared_template for provider in provider_list},
            guide_summary,
            resolved_guide_path,
            {provider: shared_path for provider in provider_list},
        )

    prompt_templates: dict[str, str] = {}
    prompt_paths: dict[str, Path] = {}
    for provider in provider_list:
        prompt_path = _provider_prompt_file(provider)
        prompt_templates[provider] = apply_guide_summary_to_prompt(
            load_prompt(prompt_path),
            guide_summary,
        )
        prompt_paths[provider] = _resolve_repo_path(prompt_path)
    return prompt_templates, guide_summary, resolved_guide_path, prompt_paths


def prompt_template_for_provider(
    prompt_template: str | dict[str, str] | None,
    provider: str,
) -> str | None:
    """
    Pick the correct template for provider-aware callers.

    Older tests and helper calls still pass a single string, so this adapter lets
    new provider-specific routing coexist with the original shared-template API.
    """
    if isinstance(prompt_template, dict):
        return prompt_template[provider]
    return prompt_template


def build_user_message(article_text: str, template: str | None = None) -> str:
    """
    Substitute the article text into the prompt template, applying
    MAX_INPUT_CHARS truncation if configured.

    The cache (data/processed/*.jsonl) holds the full cleaned paper text.
    Truncation happens HERE — at the point of LLM call — so the limit
    can be tuned via .env without re-running extraction.
    """
    if MAX_INPUT_CHARS > 0 and len(article_text) > MAX_INPUT_CHARS:
        article_text = truncate_to_limit(article_text, MAX_INPUT_CHARS)
    tmpl = template if template is not None else load_prompt()
    return tmpl.replace("{ARTICLE_TEXT}", article_text)


def build_pdf_user_message(template: str | None = None) -> str:
    """
    Build the same summarisation instructions for a directly attached PDF.

    The normal prompt has a slot named ``{ARTICLE_TEXT}`` because text-cache
    runs paste the article body into the prompt. For direct-PDF runs, the paper
    travels as a separate file attachment instead. We replace the slot with a
    plain-English pointer so every provider gets the same task instructions
    while reading the attached PDF itself.
    """
    tmpl = template if template is not None else load_prompt()
    return tmpl.replace(
        "{ARTICLE_TEXT}",
        "The article is attached as a PDF file. Read the attached PDF and use "
        "only information supported by that document.",
    )


def _openai_completion_token_arg() -> dict[str, int]:
    """
    Return the output-token parameter used by current OpenAI chat models.

    Newer reasoning-capable OpenAI models reject the older ``max_tokens`` name
    and ask for ``max_completion_tokens`` instead. Keeping the choice in one
    helper makes both real-time and batch request builders easy to update.
    """
    return {"max_completion_tokens": MAX_OUTPUT_TOKENS}


def _remove_json_schema_keys(value: Any, blocked: set[str]) -> Any:
    """
    Recursively remove JSON-schema keys unsupported by a provider SDK.

    Gemini's older ``google.generativeai`` SDK rejects Pydantic schema metadata
    such as ``default``. Removing only metadata keeps the real required fields
    intact while avoiding the SDK-side validation error.
    """
    if isinstance(value, dict):
        return {
            key: _remove_json_schema_keys(item, blocked)
            for key, item in value.items()
            if key not in blocked
        }
    if isinstance(value, list):
        return [_remove_json_schema_keys(item, blocked) for item in value]
    return value


def _gemini_coerce_nullable(schema: Any) -> Any:
    """Convert Pydantic anyOf-null pattern to Gemini's nullable flag.

    Gemini structured output does not support anyOf. For Optional[X] Pydantic
    generates {"anyOf": [{"type": "X"}, {"type": "null"}]}; Gemini requires
    {"type": "X", "nullable": true}.
    """
    if isinstance(schema, dict):
        any_of = schema.get("anyOf")
        if isinstance(any_of, list):
            non_null = [s for s in any_of if s.get("type") != "null" and s != {"type": "null"}]
            has_null = len(non_null) < len(any_of)
            if has_null and len(non_null) == 1:
                result = {**non_null[0], "nullable": True}
                for k, v in schema.items():
                    if k != "anyOf" and k not in result:
                        result[k] = v
                return _gemini_coerce_nullable(result)
        return {k: _gemini_coerce_nullable(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_gemini_coerce_nullable(item) for item in schema]
    return schema


def gemini_response_schema() -> dict[str, Any]:
    """Return a Gemini-compatible copy of the VeterinarySummary JSON schema."""
    schema = copy.deepcopy(VeterinarySummary.model_json_schema())
    # Strip keys Gemini rejects: title/anyOf/$ keys cause structured-output failures.
    stripped = _remove_json_schema_keys(
        schema, {"default", "additionalProperties", "title", "$schema", "$defs"}
    )
    return _gemini_coerce_nullable(stripped)


def _gemini_output_token_limit() -> int:
    """
    Return Gemini's completion cap.

    Gemini has been truncating otherwise valid VeterinarySummary JSON at the
    shared 1200-1500 token cap. We keep the shared cap for other providers, but
    give Gemini a larger default because its JSON output often includes escaped
    prose and pretty formatting; this is safer than trying to repair partial
    scientific summaries after truncation.
    """
    return GEMINI_MAX_OUTPUT_TOKENS


def _gemini_finish_reason(response: object) -> str:
    """Extract Gemini's finish reason across SDK object/dict shapes."""
    candidates = getattr(response, "candidates", None) or []
    candidate = candidates[0] if candidates else None
    finish = getattr(candidate, "finish_reason", None)
    if isinstance(candidate, dict):
        finish = candidate.get("finish_reason", finish)
    return str(finish or "unknown")


def _parse_gemini_veterinary_summary(response: object) -> VeterinarySummary:
    """
    Validate Gemini structured output without depending only on response.text.

    The google-genai SDK may expose schema output as ``response.parsed``. Reading
    that first avoids JSON string edge cases. If the SDK only provides text, we
    parse it normally and raise a clear truncation-oriented error instead of the
    opaque ``Unterminated string`` message.
    """
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return coerce_veterinary_summary(parsed)

    text = str(getattr(response, "text", "") or "")
    if not text:
        raise ValueError(f"Gemini returned empty response.text (finish_reason={_gemini_finish_reason(response)})")
    try:
        return coerce_veterinary_summary(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Gemini returned incomplete or invalid JSON "
            f"(finish_reason={_gemini_finish_reason(response)}, "
            f"chars={len(text)}, max_output_tokens={_gemini_output_token_limit()}). "
            "Increase GEMINI_MAX_OUTPUT_TOKENS if this recurs."
        ) from exc


# ---------------------------------------------------------------------------
# DRY_RUN mock summary (no API calls, deterministic)
# ---------------------------------------------------------------------------

def _mock_summary(model_name: str, article_text: str, title_hint: str = "") -> dict:
    """
    Generate a fake-but-schema-valid summary for DRY_RUN mode.

    Returned shape matches the real success shape so downstream code never
    has to branch on dry-run vs live.
    """
    spec = get_model_spec(model_name)
    snippet = (article_text.strip().split("\n", 1)[0])[:120]
    summary = f"[MOCK {model_name}] {title_hint or snippet or 'summary'}".strip()
    parsed = VeterinarySummary(
        headline=summary,
        objective="Not reported",
        study_design="Not reported",
        species="Not reported",
        sample_size=None,
        key_methods=["Mock dry-run method placeholder."],
        key_findings=["Mock dry-run finding placeholder."],
        clinical_significance="Mock dry-run clinical significance placeholder.",
        limitations=["Mock dry-run limitation placeholder."],
        summary_text=summary,
    )
    # Deterministic fake token counts proportional to input size so cost
    # estimates from DRY_RUN are non-zero and the smoke test catches missing
    # usage fields.
    return _successful_summary_result(
        parsed=parsed,
        input_tokens=max(100, len(article_text) // 4),
        output_tokens=120,
        model_version=f"{spec.model_id}-DRYRUN",
    )


def _mock_pdf_summary(model_name: str, pdf_path: Path) -> dict:
    """
    Free test-mode stand-in for a direct-PDF API call.

    We do not open or parse the PDF here. Test mode's job is only to prove the
    pipeline wiring is correct without spending money, so the filename is enough
    to create a deterministic fake summary with the same shape as a live result.
    """
    return _mock_summary(model_name, f"PDF input: {pdf_path.name}", title_hint=pdf_path.stem)


# ---------------------------------------------------------------------------
# Provider-specific real-time callers
# ---------------------------------------------------------------------------

def _openai_is_temperature_error(exc: Exception) -> bool:
    """Return True when exc says this OpenAI model doesn't accept temperature."""
    msg = str(exc).lower()
    return "temperature" in msg and (
        "unsupported" in msg or "not supported" in msg or "invalid" in msg
    )


def _is_non_retriable_openai_error(exc: Exception) -> bool:
    """Return True for OpenAI errors that must not be retried (400/401/403).

    Safe to call for any provider — isinstance returns False for non-OpenAI
    exceptions. Uses getattr so test mocks that omit APIStatusError don't crash.
    """
    try:
        import openai as _openai  # noqa: import-outside-toplevel
        _APIStatusError = getattr(_openai, "APIStatusError", None)
        if _APIStatusError is not None and isinstance(exc, _APIStatusError):
            return exc.status_code in (400, 401, 403)
    except ImportError:
        pass
    return False


def _is_gemini_overload(exc: Exception) -> bool:
    """True when the google-genai SDK received a 503 UNAVAILABLE response."""
    msg = str(exc)
    return "503" in msg or "UNAVAILABLE" in msg


def _openai_call_with_backoff(api_call_fn):
    """Execute api_call_fn() with jittered exponential backoff on HTTP 429.

    api_call_fn must be a zero-argument callable (lambda/closure) that returns
    an OpenAI response object. Only RateLimitError (429) is retried; all other
    exceptions propagate immediately so callers can apply their own logic.

    The openai import is lazy so unit tests that patch sys.modules["openai"]
    with a SimpleNamespace continue to work — getattr guards ensure missing
    error classes on the mock don't cause AttributeError.
    """
    import openai  # noqa: import-outside-toplevel

    _RateLimitError = getattr(openai, "RateLimitError", None)
    _APIStatusError = getattr(openai, "APIStatusError", None)

    last_exc: Exception | None = None
    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            return api_call_fn()
        except Exception as exc:
            # Non-429 HTTP errors (bad request, invalid key): re-raise at once.
            if _APIStatusError is not None and isinstance(exc, _APIStatusError):
                if not (_RateLimitError is not None and isinstance(exc, _RateLimitError)):
                    raise
            elif _RateLimitError is None or not isinstance(exc, _RateLimitError):
                # Not an openai error at all (or mock without the class).
                raise

            last_exc = exc

            # Try to honour the provider's own reset timestamp from headers.
            delay: float | None = None
            raw_response = getattr(exc, "response", None)
            if raw_response is not None:
                headers = getattr(raw_response, "headers", {}) or {}
                candidates = []
                for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                    val = headers.get(key)
                    if val:
                        try:
                            candidates.append(float(val) - time.time())
                        except (ValueError, TypeError):
                            pass
                if candidates:
                    header_wait = min(c for c in candidates if c > 0) if any(c > 0 for c in candidates) else None
                    if header_wait is not None:
                        delay = min(header_wait + random.uniform(0, 1), OPENAI_RETRY_MAX_DELAY)

            if delay is None:
                delay = min(
                    OPENAI_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1),
                    OPENAI_RETRY_MAX_DELAY,
                )

            if attempt < OPENAI_MAX_RETRIES - 1:
                print(
                    f"[phase3:summarize] OpenAI rate limit (429) on attempt {attempt + 1}; "
                    f"retrying in {delay:.1f}s"
                )
                time.sleep(delay)

    raise last_exc  # type: ignore[misc]


def _call_openai(article_text: str, *, prompt_template: str | None) -> dict:
    """
    Real-time OpenAI call. Imports the SDK lazily so unit tests that mock
    this function don't require the package to be installed.

    Newer reasoning models (o1, o3, o4-mini) reject explicit temperature.
    We try with temperature first; if the provider returns a temperature-
    related error we retry without it rather than failing the whole run.
    """
    import openai  # type: ignore[import-not-found]

    spec = get_model_spec("openai")
    user_message = build_user_message(article_text, prompt_template)
    client = openai.OpenAI()  # picks up OPENAI_API_KEY from env

    base_kwargs: dict[str, Any] = {
        "model": spec.model_id,
        "messages": [{"role": "user", "content": user_message}],
        "response_format": VeterinarySummary,
        **_openai_completion_token_arg(),
    }
    try:
        response = _openai_call_with_backoff(
            lambda: client.beta.chat.completions.parse(
                **base_kwargs, temperature=TEMPERATURE, seed=SEED
            )
        )
    except Exception as exc:
        if not _openai_is_temperature_error(exc):
            raise
        print(f"[phase3:summarize] {spec.model_id} rejected temperature — retrying without it.")
        response = _openai_call_with_backoff(
            lambda: client.beta.chat.completions.parse(**base_kwargs)
        )

    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise ValueError("OpenAI returned no parsed VeterinarySummary object.")

    return _successful_summary_result(
        parsed=coerce_veterinary_summary(parsed),
        # Pull tokens FROM the response, not estimated. This is the only way
        # BudgetGuard.total_spent will match the OpenAI dashboard charge.
        input_tokens=int(response.usage.prompt_tokens),
        output_tokens=int(response.usage.completion_tokens),
        # `response.model` is the exact version OpenAI routed to, e.g.
        # "gpt-5.4-0325-preview" — not the alias "gpt-5.4" we requested.
        # Logging this lets us detect silent model updates across runs.
        model_version=str(response.model),
        system_fingerprint=getattr(response, "system_fingerprint", None),
    )


def _call_anthropic(article_text: str, *, prompt_template: str | None) -> dict:
    """Real-time Anthropic call."""
    import anthropic  # type: ignore[import-not-found]

    spec = get_model_spec("anthropic")
    user_message = build_user_message(article_text, prompt_template)
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env

    response = client.messages.create(
        model=spec.model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": user_message}],
        tools=[{
            "name": "VeterinarySummary",
            "description": (
                "Return the veterinary article summary using exactly the "
                "VeterinarySummary schema."
            ),
            "input_schema": VeterinarySummary.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "VeterinarySummary"},
    )

    tool_input = _extract_anthropic_tool_input(response.content)
    parsed = coerce_veterinary_summary(tool_input)

    return _successful_summary_result(
        parsed=parsed,
        input_tokens=int(response.usage.input_tokens),
        output_tokens=int(response.usage.output_tokens),
        model_version=str(response.model),
    )


def _extract_anthropic_tool_input(content_blocks) -> dict:
    """
    Return the forced VeterinarySummary tool input from an Anthropic response.

    The SDK may expose blocks as typed objects or dicts depending on version and
    tests, so this helper accepts both forms.
    """
    for block in content_blocks:
        block_type = getattr(block, "type", None)
        block_name = getattr(block, "name", None)
        block_input = getattr(block, "input", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            block_name = block.get("name", block_name)
            block_input = block.get("input", block_input)
        if block_type == "tool_use" and block_name == "VeterinarySummary":
            if not isinstance(block_input, dict):
                raise ValueError("Anthropic VeterinarySummary tool input was not a dict.")
            return block_input
    raise ValueError("Anthropic response did not include the VeterinarySummary tool call.")


def _load_google_genai():
    """
    Import the modern Gemini SDK and return its client module plus types module.

    A plain ``from google import genai`` can fail when another package has
    already created the ``google`` namespace without the GenAI submodule. Using
    importlib targets ``google.genai`` directly and lets us give the user a
    concrete setup fix when the SDK is missing from the active virtualenv.
    """
    try:
        genai = importlib.import_module("google.genai")
        types = importlib.import_module("google.genai.types")
    except ImportError as exc:
        raise ImportError(
            "Gemini provider requires the google-genai package in the active "
            "virtual environment. Run: python -m pip install -r requirements.txt"
        ) from exc
    return genai, types


def _call_gemini(article_text: str, *, prompt_template: str | None) -> dict:
    """Real-time Gemini call (no batch API)."""
    genai, types = _load_google_genai()

    spec = get_model_spec("gemini")
    user_message = build_user_message(article_text, prompt_template)

    # The newer google-genai SDK reads GEMINI_API_KEY automatically, but we pass
    # it explicitly when present so .env-driven local runs are easy to reason
    # about. This replaces the deprecated google.generativeai package.
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=spec.model_id,
        contents=user_message,
        config=types.GenerateContentConfig(
            temperature=TEMPERATURE,
            max_output_tokens=_gemini_output_token_limit(),
            response_mime_type="application/json",
            response_schema=gemini_response_schema(),
        ),
    )

    usage = response.usage_metadata
    parsed = _parse_gemini_veterinary_summary(response)
    return _successful_summary_result(
        parsed=parsed,
        input_tokens=int(usage.prompt_token_count),
        output_tokens=int(usage.candidates_token_count),
        model_version=spec.model_id,  # Gemini does not return a version string.
    )


def _response_usage_int(usage: object, *names: str) -> int:
    """
    Read a token count from SDK usage objects that may use different names.

    Provider SDKs expose usage as small objects, not normal dictionaries. This
    helper keeps the token-accounting code readable while still failing loudly
    if a provider stops returning the usage field we charge against.
    """
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return int(value)
    raise AttributeError(f"Missing usage token field; tried {', '.join(names)}")


def _extract_openai_parsed_summary(response: object) -> VeterinarySummary:
    """
    Return the parsed VeterinarySummary from OpenAI's SDK response.

    Text runs use Chat Completions, while PDF runs use the newer Responses API.
    The two response shapes store the parsed object in different places, so this
    tiny adapter keeps the rest of the code provider-independent.
    """
    parsed = getattr(response, "output_parsed", None)
    if parsed is not None:
        return coerce_veterinary_summary(parsed)

    choices = getattr(response, "choices", None)
    if choices:
        parsed = getattr(choices[0].message, "parsed", None)
        if parsed is not None:
            return coerce_veterinary_summary(parsed)

    raise ValueError("OpenAI returned no parsed VeterinarySummary object.")


def _call_openai_pdf(pdf_path: Path, *, prompt_template: str | None) -> dict:
    """
    Real-time OpenAI direct-PDF call.

    We upload the PDF as ``purpose='user_data'`` and then ask the Responses API
    to fill the same Pydantic schema used for text runs. This keeps PDF and
    JSONL summaries comparable: same schema, same temperature, same output cap.
    """
    import openai  # type: ignore[import-not-found]

    spec = get_model_spec("openai")
    user_message = build_pdf_user_message(prompt_template)
    client = openai.OpenAI()

    with open(pdf_path, "rb") as f:
        upload = client.files.create(file=f, purpose="user_data")

    base_kwargs: dict[str, Any] = {
        "model": spec.model_id,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": upload.id},
                {"type": "input_text", "text": user_message},
            ],
        }],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "text_format": VeterinarySummary,
    }
    try:
        response = _openai_call_with_backoff(
            # The Responses API used for file inputs does not accept ``seed`` in
            # the installed SDK, unlike Chat Completions. Passing only supported
            # controls preserves deterministic temperature while avoiding a
            # local TypeError before the request is sent.
            lambda: client.responses.parse(**base_kwargs, temperature=TEMPERATURE)
        )
    except Exception as exc:
        if not _openai_is_temperature_error(exc):
            raise
        print(f"[phase3:summarize] {spec.model_id} rejected temperature for PDF — retrying without it.")
        response = _openai_call_with_backoff(
            lambda: client.responses.parse(**base_kwargs)
        )

    parsed = _extract_openai_parsed_summary(response)
    usage = response.usage
    return _successful_summary_result(
        parsed=parsed,
        input_tokens=_response_usage_int(usage, "input_tokens", "prompt_tokens"),
        output_tokens=_response_usage_int(usage, "output_tokens", "completion_tokens"),
        model_version=str(getattr(response, "model", spec.model_id)),
        system_fingerprint=getattr(response, "system_fingerprint", None),
    )


def _call_anthropic_pdf(pdf_path: Path, *, prompt_template: str | None) -> dict:
    """
    Real-time Anthropic direct-PDF call.

    Anthropic accepts PDFs as a base64 ``document`` block. We put that document
    block before the text instructions so the model sees the source paper first
    and then the exact schema-focused task it must complete.
    """
    import anthropic  # type: ignore[import-not-found]

    spec = get_model_spec("anthropic")
    user_message = build_pdf_user_message(prompt_template)
    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    client = anthropic.Anthropic()

    response = client.messages.create(
        model=spec.model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": user_message},
            ],
        }],
        tools=[{
            "name": "VeterinarySummary",
            "description": (
                "Return the veterinary article summary using exactly the "
                "VeterinarySummary schema."
            ),
            "input_schema": VeterinarySummary.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "VeterinarySummary"},
    )

    tool_input = _extract_anthropic_tool_input(response.content)
    return _successful_summary_result(
        parsed=coerce_veterinary_summary(tool_input),
        input_tokens=int(response.usage.input_tokens),
        output_tokens=int(response.usage.output_tokens),
        model_version=str(response.model),
    )


def _call_gemini_pdf(pdf_path: Path, *, prompt_template: str | None) -> dict:
    """
    Real-time Gemini direct-PDF call.

    Gemini's SDK uploads the PDF first, then the model receives a two-item input:
    the uploaded file and the same instructions used everywhere else. The output
    is still validated against VeterinarySummary before it is written to disk.
    """
    genai, types = _load_google_genai()

    spec = get_model_spec("gemini")
    user_message = build_pdf_user_message(prompt_template)

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    uploaded = client.files.upload(file=str(pdf_path))
    response = client.models.generate_content(
        model=spec.model_id,
        contents=[uploaded, user_message],
        config=types.GenerateContentConfig(
            temperature=TEMPERATURE,
            max_output_tokens=_gemini_output_token_limit(),
            response_mime_type="application/json",
            response_schema=gemini_response_schema(),
        ),
    )

    usage = response.usage_metadata
    parsed = _parse_gemini_veterinary_summary(response)
    return _successful_summary_result(
        parsed=parsed,
        input_tokens=int(usage.prompt_token_count),
        output_tokens=int(usage.candidates_token_count),
        model_version=spec.model_id,
    )


PROVIDER_CALLERS: dict[str, Callable[..., dict]] = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
}

PROVIDER_PDF_CALLERS: dict[str, Callable[..., dict]] = {
    "openai": _call_openai_pdf,
    "anthropic": _call_anthropic_pdf,
    "gemini": _call_gemini_pdf,
}


# ---------------------------------------------------------------------------
# Public router
# ---------------------------------------------------------------------------

def generate_summary(
    model_name: str,
    text: str,
    *,
    prompt_template: str | None = None,
    max_retries: int = 5,
) -> dict:
    """
    Route a summarisation request to the correct provider and return a
    normalised result dict.

    Retries with exponential backoff (1s, 2s, 4s) on transient errors.
    On final failure, returns {"status": "failed", "error": "<message>", ...}
    instead of raising — so a single paper's failure doesn't kill the run.
    """
    if model_name not in PROVIDER_CALLERS:
        raise KeyError(f"Unknown provider '{model_name}'")

    if _is_dry_run():
        return _mock_summary(model_name, text)

    caller = PROVIDER_CALLERS[model_name]
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return caller(text, prompt_template=prompt_template)
        except Exception as exc:
            last_error = exc
            if _is_non_retriable_openai_error(exc):
                break
            if attempt < max_retries - 1:
                if _is_gemini_overload(exc):
                    wait = min(15 * (2 ** attempt) + random.uniform(0, 2), 120.0)
                else:
                    wait = min(2 ** attempt + random.uniform(0, 1), 30.0)
                print(f"[phase3:summarize] {model_name} attempt {attempt+1} failed: {exc}; "
                      f"retrying in {wait:.1f}s")
                time.sleep(wait)

    log_error(
        doi="N/A",
        stage=f"summarize_{model_name}",
        message=f"All {max_retries} attempts failed: {last_error}",
    )
    return {
        "status": "failed",
        "error": str(last_error),
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def generate_summary_from_pdf(
    model_name: str,
    pdf_path: Path,
    *,
    prompt_template: str | None = None,
    max_retries: int = 5,
) -> dict:
    """
    Route one directly attached PDF to a provider and return the normal result.

    This is separate from ``generate_summary`` because direct PDF handling is
    provider-specific: OpenAI uploads a file ID, Anthropic sends base64, and
    Gemini uses ``upload_file``. Keeping it in one router means the run loop
    still records costs and summaries exactly the same way for all input types.
    """
    if model_name not in PROVIDER_PDF_CALLERS:
        raise KeyError(f"Unknown provider '{model_name}'")

    if _is_dry_run():
        return _mock_pdf_summary(model_name, pdf_path)

    caller = PROVIDER_PDF_CALLERS[model_name]
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return caller(pdf_path, prompt_template=prompt_template)
        except Exception as exc:
            last_error = exc
            if _is_non_retriable_openai_error(exc):
                break
            if attempt < max_retries - 1:
                if _is_gemini_overload(exc):
                    wait = min(15 * (2 ** attempt) + random.uniform(0, 2), 120.0)
                else:
                    wait = min(2 ** attempt + random.uniform(0, 1), 30.0)
                print(f"[phase3:summarize] {model_name} PDF attempt {attempt+1} failed: {exc}; "
                      f"retrying in {wait:.1f}s")
                time.sleep(wait)

    log_error(
        doi="N/A",
        stage=f"summarize_pdf_{model_name}",
        message=f"All {max_retries} PDF attempts failed: {last_error}",
    )
    return {
        "status": "failed",
        "error": str(last_error),
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Persistence: data/summaries.jsonl
# ---------------------------------------------------------------------------

def _empty_model_slot() -> dict:
    return {
        "status": "pending",
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": None,
    }


def _custom_id_for_source(doi: str, input_source: str) -> str:
    """Keep legacy custom_id for processed text; suffix raw comparison rows."""
    slug = doi_to_slug(doi)
    return slug if input_source == "processed" else f"{slug}__{input_source}"


def _summary_key(doi: str, input_source: str) -> str:
    return f"{doi}::{input_source}"


def _entry_key(entry: dict) -> str:
    doi = str(entry.get("doi", "")).strip()
    source = str(entry.get("input_source") or "processed")
    return _summary_key(doi, source)


def _new_summary_entry(record: dict, input_source: str = "processed") -> dict:
    """Build an empty summary row from a manifest record."""
    doi = str(record.get("doi", "")).strip()
    return {
        "doi": doi,
        "custom_id": _custom_id_for_source(doi, input_source),
        "input_source": input_source,
        "journal": record.get("journal"),
        "species": record.get("species"),
        "study_design": record.get("study_design"),
        "clinical_topic": record.get("clinical_topic"),
        "title": record.get("title"),
        "models": {p: _empty_model_slot() for p in all_providers()},
    }


def load_existing_summaries(path: Path | None = None) -> dict[str, dict]:
    """Load existing summaries keyed by DOI + input source for resume support."""
    resolved = path if path is not None else SUMMARIES_PATH
    if not resolved.exists():
        return {}
    out: dict[str, dict] = {}
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(entry.get("doi", "")).strip()
            if doi:
                out[_entry_key(entry)] = entry
    return out


def _write_all_summaries(path: Path, summaries_by_doi: dict[str, dict]) -> None:
    """
    Rewrite the summaries file from scratch.

    Phase 2 conventions are append-only, but `--resume` requires merging
    per-(doi,model) results into an existing row. The compromise: we
    rewrite the whole file in one atomic step at the end of each run. Each
    line is still valid JSON, so any partial-completion analysis still works
    on the snapshot from before the rewrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in summaries_by_doi.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Confirmation guard
# ---------------------------------------------------------------------------

def confirm_real_batch(profile: ModeProfile, force: bool) -> bool:
    """
    Final interactive guardrail. Returns True if it's safe to proceed.

    Triggers when the resolved profile both requires confirmation AND is
    not already short-circuited to mocks. ``--force`` bypasses the prompt
    for overnight/scheduled use and logs an audit line.
    """
    if profile.dry_run or not profile.requires_confirm:
        return True

    if force:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        audit_line = (
            f"[phase3:safety] confirmation bypassed via --force at "
            f"{datetime.now(timezone.utc).isoformat()}\n"
        )
        with open(LOGS_DIR / "phase3_safety.log", "a", encoding="utf-8") as f:
            f.write(audit_line)
        print(audit_line.strip())
        return True

    label = "REAL batch jobs to paid APIs" if profile.use_batch else (
        f"REAL real-time API calls (limit={profile.paper_limit or 'no limit'})"
    )
    reply = input(
        f"\n[phase3:safety] About to submit {label}.\n"
        "Type 'yes' to confirm: "
    ).strip().lower()
    if reply != "yes":
        print("[phase3:safety] Confirmation not received; aborting.")
        return False
    return True


# ---------------------------------------------------------------------------
# Real-time run loop
# ---------------------------------------------------------------------------

def _iter_manifest(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_cached_text(record_or_doi, input_source: str = "processed") -> str | None:
    """
    Locate this paper's cleaned-text cache and return its body.

    Delegates to ``prepare_texts.read_cached_text`` so both descriptive
    (``jvim__title__10_1111_jvim_16872.jsonl``) and legacy (``10_1111_jvim_16872.jsonl``)
    cache filenames work — the summariser doesn't care which the prepare
    step used. Accepts a full manifest record (preferred, enables descriptive
    lookup) or a bare DOI string (legacy fallback only).
    """
    from prepare_texts import read_cached_text as _shared_read
    return _shared_read(record_or_doi, input_source=input_source)


def _read_pdf_path(record_or_doi) -> Path | None:
    """
    Locate the source PDF for a manifest record.

    The shared path helper checks both current descriptive filenames and older
    DOI-only filenames, so users do not have to rename existing files before
    trying a direct-PDF comparison.
    """
    return resolve_existing_pdf_path(RAW_DIR, record_or_doi)


def run_realtime(
    *,
    manifest_path: Path = MANIFEST_PATH,
    resume: bool = False,
    paper_limit: int | None = None,
    providers: list[str] | None = None,
    input_source: str = "processed",
    guide_summary_path: Path | None = GUIDE_SUMMARY_FILE,
    doi_filter: set[str] | None = None,
) -> dict[str, int]:
    """
    Sequentially summarise each paper across each provider in real time.

    ``doi_filter``, when provided, restricts the run to exactly those DOIs
    (used by dev-mode journal-stratified selection in run_phase3.py). Rows
    outside the filter are skipped silently — not counted anywhere in
    ``counts`` — since they were never selected for this run in the first
    place. ``paper_limit`` is ignored whenever a filter is given: the filter
    set already says precisely how many/which papers run, and manifest order
    isn't guaranteed to match selection order, so a paper_limit early-break
    could cut the loop off before every filtered DOI is reached.
    """
    providers = providers or list(all_providers())
    prompt_templates, guide_summary, resolved_guide_path, _prompt_paths = (
        load_provider_prompt_templates_with_optional_guide(providers, guide_summary_path)
    )
    if guide_summary:
        print(f"[phase3:summarize] using format guide: {resolved_guide_path}")
    # Always keep rows from other papers/input sources. ``resume`` only decides
    # whether already-successful model slots are skipped or refreshed.
    existing = load_existing_summaries()
    guard = BudgetGuard()

    counts = {"success": 0, "failed": 0, "skipped": 0, "no_text": 0}
    processed_papers = 0

    summaries_by_doi: dict[str, dict] = dict(existing)

    for record in _iter_manifest(manifest_path):
        record_doi = str(record.get("doi", "")).strip()
        if doi_filter is not None and record_doi not in doi_filter:
            continue
        if doi_filter is None and paper_limit is not None and processed_papers >= paper_limit:
            break

        doi = record_doi
        if not doi:
            continue

        slug = doi_to_slug(doi)
        # Pass the full manifest record so we can find the descriptive-named
        # cache file. Falls back to legacy {slug}.jsonl if a record is
        # missing journal/title or the migration hasn't happened yet.
        article_text: str | None = None
        pdf_path: Path | None = None
        if input_source == "pdf":
            pdf_path = _read_pdf_path(record)
        else:
            # Pass the full manifest record so we can find the descriptive-named
            # cache file. Falls back to legacy {slug}.jsonl if a record is
            # missing journal/title or the migration hasn't happened yet.
            article_text = _read_cached_text(record, input_source=input_source)

        if input_source == "pdf" and pdf_path is None:
            counts["no_text"] += 1
            log_error(doi, "summarize",
                      f"No PDF file found for {slug} in data/raw (looked for "
                      "descriptive name + legacy DOI filename)")
            continue
        if input_source != "pdf" and article_text is None:
            counts["no_text"] += 1
            log_error(doi, "summarize",
                      f"No {input_source} cached text for {slug} (looked for "
                      f"descriptive name + legacy {slug}.jsonl)")
            continue

        key = _summary_key(doi, input_source)
        entry = summaries_by_doi.get(key) or _new_summary_entry(record, input_source)
        summaries_by_doi[key] = entry

        processed_papers += 1
        print(f"\n[phase3:summarize] paper {processed_papers}: {doi}")

        for provider in providers:
            slot = entry["models"].setdefault(provider, _empty_model_slot())
            if resume and slot.get("status") == "success":
                counts["skipped"] += 1
                print(f"  {provider}: already success, skipping")
                continue

            sleep_for_model(provider)
            provider_prompt = prompt_template_for_provider(prompt_templates, provider)
            if input_source == "pdf":
                # pdf_path is known to be present because missing PDFs were
                # skipped above. This branch is the only place live direct-PDF
                # calls happen, which keeps batch/dev runs safely text-only.
                result = generate_summary_from_pdf(
                    provider, pdf_path, prompt_template=provider_prompt
                )
            else:
                result = generate_summary(
                    provider, article_text, prompt_template=provider_prompt
                )

            entry["models"][provider] = result

            if result["status"] == "success":
                counts["success"] += 1
                cost = compute_cost(
                    provider,
                    int(result["input_tokens"] or 0),
                    int(result["output_tokens"] or 0),
                    batched=False,
                )
                guard.add_cost(cost)
                print(f"  {provider}: success "
                      f"(in={result['input_tokens']}, out={result['output_tokens']}, "
                      f"ver={result['model_version']}, ${cost:.4f})")
            else:
                counts["failed"] += 1
                print(f"  {provider}: FAILED — {result.get('error')}")

    _write_all_summaries(SUMMARIES_PATH, summaries_by_doi)
    print(f"\n[phase3:summarize] done. counts={counts}  budget_spent=${guard.total_spent:.4f}")
    return counts


# ---------------------------------------------------------------------------
# Folder-based summarisation (summarize-all workflow)
# ---------------------------------------------------------------------------

class SummaryRunStats(dict[str, int]):
    """Counts dict plus the actual USD spent by that summarize-all source run."""

    def __init__(self, counts: dict[str, int], *, budget_spent: float) -> None:
        super().__init__(counts)
        self.budget_spent = budget_spent


def _format_human_readable(structured: dict) -> str:
    """
    Format a VeterinarySummary structured dict into numbered clinical sections.

    Sections always appear in this fixed order so every output looks the same
    regardless of which provider produced it. Lists are bullet-pointed; scalar
    fields are left as prose.
    """
    parts: list[str] = []

    objective = str(structured.get("objective") or "Not reported").strip()
    parts.append(f"1. Objective\n{objective}")

    methods = structured.get("key_methods") or []
    if isinstance(methods, list):
        body = "\n".join(f"- {m}" for m in methods) if methods else "Not reported"
    else:
        body = str(methods)
    parts.append(f"2. Key Methods\n{body}")

    findings = structured.get("key_findings") or []
    if isinstance(findings, list):
        body = "\n".join(f"- {f}" for f in findings) if findings else "Not reported"
    else:
        body = str(findings)
    parts.append(f"3. Primary Results\n{body}")

    significance = str(structured.get("clinical_significance") or "Not reported").strip()
    parts.append(f"4. Clinical Significance\n{significance}")

    limitations = structured.get("limitations") or []
    if isinstance(limitations, list):
        body = "\n".join(f"- {l}" for l in limitations) if limitations else "Not reported"
    else:
        body = str(limitations)
    parts.append(f"5. Limitations\n{body}")

    return "\n\n".join(parts)


def _enrich_result_with_human_readable(result: dict) -> dict:
    """Attach a human_readable field to a successful provider result."""
    structured = result.get("structured_summary")
    if structured and result.get("status") == "success":
        result = dict(result)
        result["human_readable"] = _format_human_readable(structured)
    return result


def _format_provider_section(provider: str, result: dict) -> list[str]:
    """
    Build the "=== PROVIDER SUMMARY ===" block shared by every folder-based
    readable-text output (summarize-all's PDF/processed comparison files and
    the dev-mode jsonl-only files both use this).

    Falls back to computing the readable text from ``structured_summary`` when
    ``human_readable`` isn't already attached to the result — summarize-all
    calls ``_enrich_result_with_human_readable`` before this runs, but
    ``run_realtime`` (the plain ``summarize`` pipeline, data/summaries.jsonl)
    does not, since that file's schema is left untouched on purpose.
    """
    lines = [
        "",
        "=" * 78,
        f"{provider.upper()} SUMMARY",
        "=" * 78,
        f"Status: {result.get('status') or 'unknown'}",
        f"Model Version: {result.get('model_version') or 'Not recorded'}",
        f"Timestamp: {result.get('timestamp') or 'Not recorded'}",
    ]

    if result.get("status") != "success":
        lines.extend([
            "",
            f"No readable summary was produced. Error: {result.get('error') or 'Not recorded'}",
        ])
        return lines

    readable = result.get("human_readable")
    if not readable and result.get("structured_summary"):
        readable = _format_human_readable(result["structured_summary"])
    readable = readable or result.get("summary") or "No summary text returned."
    lines.extend([
        f"Input Tokens: {result.get('input_tokens') or 'Not recorded'}",
        f"Output Tokens: {result.get('output_tokens') or 'Not recorded'}",
        "",
        str(readable).strip(),
    ])
    return lines


def _format_folder_entry_as_text(entry: dict) -> str:
    """
    Turn one multi-provider summary entry into a plain-English report.

    The folder workflow is for manual inspection, so it writes one readable text
    file per source article instead of exposing the research JSON payload first.
    The structured fields still guide this report so the sections are consistent
    across providers and easy to compare by eye.
    """
    source_type = str(entry.get("source_type") or "unknown").replace("_", " ").title()
    lines = [
        f"Summary Source: {source_type}",
        f"Source File: {entry.get('source_filename') or 'Not recorded'}",
        f"DOI: {entry.get('doi') or 'Not recorded'}",
        f"Slug: {entry.get('slug') or 'Not recorded'}",
        f"Generated At: {entry.get('generated_at') or 'Not recorded'}",
        "",
        "This file contains one summary from each configured model provider.",
        "Compare the provider sections below against the same article source.",
    ]

    models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
    for provider in all_providers():
        result = models.get(provider) if isinstance(models.get(provider), dict) else _empty_model_slot()
        lines.extend(_format_provider_section(provider, result))

    return "\n".join(lines).rstrip() + "\n"


def _format_dev_summary_entry_as_text(entry: dict) -> str:
    """
    Turn one ``data/summaries.jsonl``-shaped entry (built by
    ``_new_summary_entry``) into a plain-English report for
    ``data/dev_summaries_jsonl/``.

    This is the readable sibling of the real dev-mode ``summarize`` run: no
    PDF is involved (see ``_format_folder_entry_as_text`` for the PDF-vs-
    processed-text comparison version used by summarize-all), so the header
    surfaces the manifest metadata (journal/species/study design/clinical
    topic) that's actually available on this entry shape instead of a source
    filename/slug that summarize's entries don't carry.
    """
    lines = [
        f"DOI: {entry.get('doi') or 'Not recorded'}",
        f"Journal: {entry.get('journal') or 'Not recorded'}",
        f"Title: {entry.get('title') or 'Not recorded'}",
        f"Species: {entry.get('species') or 'Not recorded'}",
        f"Study Design: {entry.get('study_design') or 'Not recorded'}",
        f"Clinical Topic: {entry.get('clinical_topic') or 'Not recorded'}",
        f"Input Source: {entry.get('input_source') or 'processed'}",
        "",
        "This file contains one summary from each configured model provider,",
        "picked for the dev-mode journal-random sample (see PHASE3_DEV_SAMPLE_SEED",
        "in .env.template). It mirrors data/summaries.jsonl for this DOI in a",
        "human-readable form; the JSONL file remains the source of truth.",
    ]

    models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
    for provider in all_providers():
        result = models.get(provider) if isinstance(models.get(provider), dict) else _empty_model_slot()
        lines.extend(_format_provider_section(provider, result))

    return "\n".join(lines).rstrip() + "\n"


def _write_folder_output(
    output_dir: Path,
    stem: str,
    entry: dict,
    *,
    output_suffix: str | None = None,
    formatter: Callable[[dict], str] = _format_folder_entry_as_text,
) -> Path:
    """Atomically write one paper's multi-model summary to the output folder."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_stem = f"{stem}__{output_suffix}" if output_suffix else stem
    out_path = output_dir / f"{output_stem}.txt"
    tmp_path = out_path.with_suffix(".txt.tmp")
    tmp_path.write_text(formatter(entry), encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def _load_folder_output(output_dir: Path, stem: str) -> dict | None:
    """Load legacy JSON folder output for resume support, or return None."""
    path = output_dir / f"{stem}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_dev_summary_jsonl_outputs(
    dois: set[str],
    *,
    output_dir: Path,
    input_source: str = "processed",
    output_suffix: str | None = None,
    summaries_path: Path | None = None,
) -> int:
    """
    Render a plain-English ``.txt`` file per DOI into ``data/dev_summaries_jsonl/``.

    This is the readable sibling of a dev-mode ``summarize`` run: it reads
    back the entries ``run_realtime`` just wrote to ``data/summaries.jsonl``
    (via ``load_existing_summaries``) for the given ``dois`` + ``input_source``,
    and writes one file per paper so a human can sanity-check the run without
    parsing JSON. It never modifies ``data/summaries.jsonl`` itself — that file
    stays the single source of truth (see CLAUDE.md's Phase 3 rules).

    Returns the number of files written, so the caller can print a summary.
    """
    existing = load_existing_summaries(summaries_path)
    written = 0
    for entry in existing.values():
        if str(entry.get("doi", "")).strip() not in dois:
            continue
        if str(entry.get("input_source") or "processed") != input_source:
            continue
        stem = descriptive_stem(entry)
        _write_folder_output(
            output_dir, stem, entry,
            output_suffix=output_suffix,
            formatter=_format_dev_summary_entry_as_text,
        )
        written += 1
    return written


def _slug_from_source_stem(stem: str) -> str:
    """
    Return the stable DOI slug embedded in descriptive corpus filenames.

    Current files use ``journal__title__doi_slug`` stems, while older files may
    be named by DOI slug alone. Keeping the source filename as the output stem
    preserves the PDF/text pairing; this helper only supplies the internal join
    key used inside each JSON payload.
    """
    parts = stem.split("__")
    return parts[-1] if len(parts) > 1 else stem


def _iter_processed_text_files(processed_dir: Path) -> list[dict[str, Any]]:
    """
    Read every ``data/processed/*.jsonl`` file with its filesystem metadata.

    ``prepare_texts.iter_processed_texts`` yields only ``(slug, text)``, which is
    enough for cost estimation but loses the real descriptive filename. The
    summarize-all workflow needs that filename so each processed-text output can
    mirror the matching PDF output by article stem.
    """
    records: list[dict[str, Any]] = []
    if not processed_dir.exists():
        return records

    for path in sorted(processed_dir.glob("*.jsonl")):
        try:
            line = path.read_text(encoding="utf-8").strip()
            payload = json.loads(line) if line else {}
        except (OSError, json.JSONDecodeError):
            log_error(path.stem, "summarize_all_processed", f"Could not read {path.name}")
            continue

        text = payload.get("text") if isinstance(payload, dict) else None
        if not (isinstance(text, str) and text.strip()):
            log_error(path.stem, "summarize_all_processed", f"No text field in {path.name}")
            continue

        slug = payload.get("slug") if isinstance(payload, dict) else None
        records.append({
            "path": path,
            "stem": path.stem,
            "source_filename": path.name,
            "doi": payload.get("doi") if isinstance(payload, dict) else None,
            "slug": slug if isinstance(slug, str) and slug else _slug_from_source_stem(path.stem),
            "text": text,
        })

    return records


def summarize_all_pdfs(
    *,
    output_dir: Path | None = None,
    providers: list[str] | None = None,
    resume: bool = False,
    prompt_template: str | dict[str, str] | None = None,
    limit: int | None = None,
    raw_dir: Path | None = None,
    stems: set[str] | None = None,
    output_suffix: str | None = None,
) -> SummaryRunStats:
    """
    Summarise every PDF in data/raw/ using all three providers.

    For each PDF the result is written to data/summaries_pdf/<stem>.txt
    with one plain-English section per provider. Failed provider slots are
    recorded in the output but do not stop other providers or other PDFs from
    running.
    """
    out_dir = output_dir if output_dir is not None else SUMMARIES_PDF_DIR
    pdf_dir = raw_dir if raw_dir is not None else RAW_DIR
    provider_list = providers if providers is not None else list(all_providers())

    counts: dict[str, int] = {"success": 0, "failed": 0, "skipped": 0, "no_source": 0}
    guard = BudgetGuard()

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[phase3:summarize-all] No PDFs found in {pdf_dir}")
        return SummaryRunStats(counts, budget_spent=guard.total_spent)

    processed = 0
    for pdf_path in pdfs:
        if stems is not None and pdf_path.stem not in stems:
            continue
        if limit is not None and processed >= limit:
            break

        stem = pdf_path.stem
        existing = _load_folder_output(out_dir, stem) if resume else None

        entry: dict = existing or {
            "source_type": "pdf",
            "source_filename": pdf_path.name,
            "doi": None,
            "slug": _slug_from_source_stem(stem),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": {p: _empty_model_slot() for p in all_providers()},
        }
        entry.setdefault("models", {p: _empty_model_slot() for p in all_providers()})

        processed += 1
        print(f"\n[phase3:summarize-all] PDF {processed}: {pdf_path.name}")

        for provider in provider_list:
            slot = entry["models"].setdefault(provider, _empty_model_slot())
            if resume and slot.get("status") == "success":
                counts["skipped"] += 1
                print(f"  {provider}: already success, skipping")
                continue

            sleep_for_model(provider)
            provider_prompt = prompt_template_for_provider(prompt_template, provider)
            try:
                result = generate_summary_from_pdf(
                    provider, pdf_path, prompt_template=provider_prompt
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                    "summary": None,
                    "structured_summary": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "model_version": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

            result = _enrich_result_with_human_readable(result)
            entry["models"][provider] = result

            if result["status"] == "success":
                counts["success"] += 1
                cost = compute_cost(
                    provider,
                    int(result["input_tokens"] or 0),
                    int(result["output_tokens"] or 0),
                    batched=False,
                )
                guard.add_cost(cost)
                print(f"  {provider}: success "
                      f"(in={result['input_tokens']}, out={result['output_tokens']}, "
                      f"ver={result['model_version']}, ${cost:.4f})")
            else:
                counts["failed"] += 1
                print(f"  {provider}: FAILED — {result.get('error')}")

        written_path = _write_folder_output(
            out_dir, stem, entry, output_suffix=output_suffix
        )
        print(f"  wrote: {written_path}")

    print(f"\n[phase3:summarize-all] PDFs done. counts={counts}  "
          f"budget_spent=${guard.total_spent:.4f}")
    return SummaryRunStats(counts, budget_spent=guard.total_spent)


def summarize_all_processed_texts(
    *,
    output_dir: Path | None = None,
    providers: list[str] | None = None,
    resume: bool = False,
    prompt_template: str | dict[str, str] | None = None,
    limit: int | None = None,
    processed_dir: Path | None = None,
    stems: set[str] | None = None,
    output_suffix: str | None = None,
) -> SummaryRunStats:
    """
    Summarise every processed JSONL file in data/processed/ using all three providers.

    For each file the result is written to data/summaries_txt/<source-stem>.txt
    with one plain-English section per provider. Failed provider slots are
    recorded in the output but do not stop other providers or other files from
    running.
    """
    out_dir = output_dir if output_dir is not None else SUMMARIES_TXT_DIR
    pdir = processed_dir if processed_dir is not None else PROCESSED_DIR
    provider_list = providers if providers is not None else list(all_providers())

    counts: dict[str, int] = {"success": 0, "failed": 0, "skipped": 0, "no_source": 0}
    guard = BudgetGuard()

    records = _iter_processed_text_files(pdir)
    if not records:
        print(f"[phase3:summarize-all] No processed JSONL files found in {pdir}")
        return SummaryRunStats(counts, budget_spent=guard.total_spent)

    processed_count = 0
    for record in records:
        if stems is not None and str(record["stem"]) not in stems:
            continue
        if limit is not None and processed_count >= limit:
            break

        stem = str(record["stem"])
        existing = _load_folder_output(out_dir, stem) if resume else None

        entry: dict = existing or {
            "source_type": "processed_text",
            "source_filename": record["source_filename"],
            "doi": record["doi"],
            "slug": record["slug"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": {p: _empty_model_slot() for p in all_providers()},
        }
        entry.setdefault("models", {p: _empty_model_slot() for p in all_providers()})

        processed_count += 1
        print(f"\n[phase3:summarize-all] Text {processed_count}: {record['source_filename']}")

        for provider in provider_list:
            slot = entry["models"].setdefault(provider, _empty_model_slot())
            if resume and slot.get("status") == "success":
                counts["skipped"] += 1
                print(f"  {provider}: already success, skipping")
                continue

            sleep_for_model(provider)
            provider_prompt = prompt_template_for_provider(prompt_template, provider)
            try:
                result = generate_summary(
                    provider, str(record["text"]), prompt_template=provider_prompt
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                    "summary": None,
                    "structured_summary": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "model_version": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

            result = _enrich_result_with_human_readable(result)
            entry["models"][provider] = result

            if result["status"] == "success":
                counts["success"] += 1
                cost = compute_cost(
                    provider,
                    int(result["input_tokens"] or 0),
                    int(result["output_tokens"] or 0),
                    batched=False,
                )
                guard.add_cost(cost)
                print(f"  {provider}: success "
                      f"(in={result['input_tokens']}, out={result['output_tokens']}, "
                      f"ver={result['model_version']}, ${cost:.4f})")
            else:
                counts["failed"] += 1
                print(f"  {provider}: FAILED — {result.get('error')}")

        written_path = _write_folder_output(
            out_dir, stem, entry, output_suffix=output_suffix
        )
        print(f"  wrote: {written_path}")

    print(f"\n[phase3:summarize-all] Texts done. counts={counts}  "
          f"budget_spent=${guard.total_spent:.4f}")
    return SummaryRunStats(counts, budget_spent=guard.total_spent)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 — multi-model summariser.")
    parser.add_argument("--mode", choices=VALID_MODES, default=None,
                        help="Override PHASE3_MODE from .env: test|single|dev|batch.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override the mode's default paper_limit (e.g. --limit 3).")
    parser.add_argument("--resume", action="store_true",
                        help="Skip (doi, model) pairs already at status=success.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the interactive confirmation. USE WITH CAUTION.")
    parser.add_argument("--providers", default=",".join(all_providers()),
                        help="Comma-separated provider keys (subset of openai,anthropic,gemini).")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--input-source", choices=VALID_INPUT_SOURCES, default="processed",
                        help="Input to summarize: processed (default), raw_text, or pdf.")
    parser.add_argument("--guide-summary", type=Path, default=GUIDE_SUMMARY_FILE,
                        help=("Optional human-written format guide. If the file exists and "
                              "has text, it is used for structure only, never as source facts."))
    parser.add_argument("--doi-filter", default=None,
                        help=("Comma-separated DOIs to restrict this run to, bypassing "
                              "--limit's sequential slicing. Set by run_phase3.py's dev-mode "
                              "journal-stratified selection; not usually passed by hand."))
    args = parser.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    doi_filter = (
        {d.strip() for d in args.doi_filter.split(",") if d.strip()}
        if args.doi_filter else None
    )

    profile = resolve_mode(args.mode)
    paper_limit = args.limit if args.limit is not None else profile.paper_limit

    print(profile.banner())
    if args.limit is not None:
        print(f"[phase3:summarize] --limit {args.limit} overrides mode default.")
    if args.input_source == "raw_text" and profile.name not in {"test", "single", "dev"}:
        print("[phase3:summarize] raw_text input is limited to test/single/dev comparison runs.")
        return 1
    if args.input_source == "pdf" and profile.name not in {"test", "single"}:
        print("[phase3:summarize] direct PDF input is limited to test/single comparison runs.")
        return 1
    if args.input_source == "pdf" and profile.use_batch:
        print("[phase3:summarize] direct PDF input is real-time only; batch mode is not supported.")
        return 1
    print(f"[phase3:summarize] input_source={args.input_source}")

    try:
        _prompt_templates, guide_summary, resolved_guide_path, _prompt_paths = (
            load_provider_prompt_templates_with_optional_guide(providers, args.guide_summary)
        )
    except Exception as exc:
        print(f"[phase3:summarize] prompt validation failed: {exc}")
        return 1
    if guide_summary:
        print(f"[phase3:summarize] format guide ready: {resolved_guide_path}")

    if not confirm_real_batch(profile, force=args.force):
        return 1
    require_positive_budget_for_real_run(
        dry_run=profile.dry_run,
        context="Phase 3 summarization",
    )

    if profile.use_batch:
        # Lazy import so unit tests don't need the batch dependencies loaded.
        from batch_utils import run_batch_summarisation
        run_batch_summarisation(
            manifest_path=args.manifest,
            resume=args.resume,
            providers=providers,
            guide_summary_path=args.guide_summary,
        )
        return 0

    run_realtime(
        manifest_path=args.manifest,
        resume=args.resume,
        paper_limit=paper_limit,
        providers=providers,
        input_source=args.input_source,
        guide_summary_path=args.guide_summary,
        doi_filter=doi_filter,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
