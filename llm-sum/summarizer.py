"""
llm-sum/summarizer.py — Multi-model summarisation router
==========================================================

This module is the heart of Phase 3's "fair comparison" guarantee. Every
provider (OpenAI, Anthropic, Gemini) is called with:
    - the same prompt (loaded from PROMPT_FILE),
    - the same temperature (0.0) and seed (42) where supported,
    - the same max output tokens.

Every successful call returns the SAME dict shape so downstream code never
branches on provider:

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
  (e.g. "gpt-5.5-0325-preview", not "gpt-5"). Providers silently update
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
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from models_config import all_providers, compute_cost, get_model_spec  # noqa: E402
from file_paths import doi_to_slug, resolve_existing_pdf_path  # noqa: E402
from utils import BudgetGuard, log_error, sleep_for_model  # noqa: E402
from extract import truncate_to_limit  # noqa: E402
from phase3_mode import ModeProfile, resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
RAW_DIR = DATA_DIR / "raw"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))
PROMPT_FILE = Path(os.getenv("PROMPT_FILE", "llm-sum/prompts/summarization_v1.txt"))
GUIDE_SUMMARY_FILE = Path(
    os.getenv("GUIDE_SUMMARY_FILE", "llm-sum/prompts/guide_summary_template.txt")
)

# Maximum characters of article text sent to the LLM. 0 = no limit (use
# full cached text). Set via MAX_INPUT_CHARS in .env to tune cost vs coverage.
# 40 000 chars ≈ 10 000 tokens ≈ a full 15-page research article.
MAX_INPUT_CHARS: int = int(os.getenv("MAX_INPUT_CHARS", "0"))
VALID_INPUT_SOURCES = ("processed", "raw_text", "pdf")


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
) -> dict:
    """Wrap a validated summary with provider metadata used downstream."""
    result = veterinary_summary_to_result(parsed)
    result.update({
        "status": "success",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_version": model_version,
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


# Backwards-compatible module attribute. Tests that monkeypatch
# `summarizer.DRY_RUN` still work; runtime code goes through `_is_dry_run()`.
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

def _call_openai(article_text: str, *, prompt_template: str | None) -> dict:
    """
    Real-time OpenAI call. Imports the SDK lazily so unit tests that mock
    this function don't require the package to be installed.
    """
    import openai  # type: ignore[import-not-found]

    spec = get_model_spec("openai")
    user_message = build_user_message(article_text, prompt_template)
    client = openai.OpenAI()  # picks up OPENAI_API_KEY from env

    response = client.beta.chat.completions.parse(
        model=spec.model_id,
        messages=[{"role": "user", "content": user_message}],
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
        seed=SEED,
        response_format=VeterinarySummary,
    )

    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise ValueError("OpenAI returned no parsed VeterinarySummary object.")

    return _successful_summary_result(
        parsed=VeterinarySummary.model_validate(parsed),
        # Pull tokens FROM the response, not estimated. This is the only way
        # BudgetGuard.total_spent will match the OpenAI dashboard charge.
        input_tokens=int(response.usage.prompt_tokens),
        output_tokens=int(response.usage.completion_tokens),
        # `response.model` is the exact version OpenAI routed to, e.g.
        # "gpt-5.5-0325-preview" — not the alias "gpt-5.5" we requested.
        # Logging this lets us detect silent model updates across runs.
        model_version=str(response.model),
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
    parsed = VeterinarySummary(**tool_input)

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


def _call_gemini(article_text: str, *, prompt_template: str | None) -> dict:
    """Real-time Gemini call (no batch API)."""
    import google.generativeai as genai  # type: ignore[import-not-found]

    spec = get_model_spec("gemini")
    user_message = build_user_message(article_text, prompt_template)

    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel(spec.model_id)
    response = model.generate_content(
        user_message,
        generation_config={
            "temperature": TEMPERATURE,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
            "response_schema": VeterinarySummary,
            # Gemini exposes a `seed` parameter via REST but the Python SDK
            # may ignore it; we still set TEMPERATURE=0.0 for determinism.
        },
    )

    usage = response.usage_metadata
    parsed = VeterinarySummary.model_validate_json(response.text)
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
        return VeterinarySummary.model_validate(parsed)

    choices = getattr(response, "choices", None)
    if choices:
        parsed = getattr(choices[0].message, "parsed", None)
        if parsed is not None:
            return VeterinarySummary.model_validate(parsed)

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

    response = client.responses.parse(
        model=spec.model_id,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": upload.id},
                {"type": "input_text", "text": user_message},
            ],
        }],
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text_format=VeterinarySummary,
    )

    parsed = _extract_openai_parsed_summary(response)
    usage = response.usage
    return _successful_summary_result(
        parsed=parsed,
        input_tokens=_response_usage_int(usage, "input_tokens", "prompt_tokens"),
        output_tokens=_response_usage_int(usage, "output_tokens", "completion_tokens"),
        model_version=str(getattr(response, "model", spec.model_id)),
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
        parsed=VeterinarySummary(**tool_input),
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
    import google.generativeai as genai  # type: ignore[import-not-found]

    spec = get_model_spec("gemini")
    user_message = build_pdf_user_message(prompt_template)

    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    uploaded = genai.upload_file(path=str(pdf_path), mime_type="application/pdf")
    model = genai.GenerativeModel(spec.model_id)
    response = model.generate_content(
        [uploaded, user_message],
        generation_config={
            "temperature": TEMPERATURE,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
            "response_schema": VeterinarySummary,
        },
    )

    usage = response.usage_metadata
    parsed = VeterinarySummary.model_validate_json(response.text)
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
    max_retries: int = 3,
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

    # Tests may monkeypatch `summarizer.DRY_RUN` directly; production code
    # may toggle the env between runs. Honour either.
    if DRY_RUN or _is_dry_run():
        return _mock_summary(model_name, text)

    caller = PROVIDER_CALLERS[model_name]
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return caller(text, prompt_template=prompt_template)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[phase3:summarize] {model_name} attempt {attempt+1} failed: {exc}; "
                      f"retrying in {wait}s")
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
    max_retries: int = 3,
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

    if DRY_RUN or _is_dry_run():
        return _mock_pdf_summary(model_name, pdf_path)

    caller = PROVIDER_PDF_CALLERS[model_name]
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return caller(pdf_path, prompt_template=prompt_template)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[phase3:summarize] {model_name} PDF attempt {attempt+1} failed: {exc}; "
                      f"retrying in {wait}s")
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
) -> dict[str, int]:
    """
    Sequentially summarise each paper across each provider in real time.
    """
    providers = providers or list(all_providers())
    guide_summary_path = guide_summary_path or GUIDE_SUMMARY_FILE
    guide_summary = load_optional_guide_summary(guide_summary_path)
    prompt_template = apply_guide_summary_to_prompt(load_prompt(), guide_summary)
    if guide_summary:
        print(f"[phase3:summarize] using format guide: {_resolve_repo_path(guide_summary_path)}")
    # Always keep rows from other papers/input sources. ``resume`` only decides
    # whether already-successful model slots are skipped or refreshed.
    existing = load_existing_summaries()
    guard = BudgetGuard()

    counts = {"success": 0, "failed": 0, "skipped": 0, "no_text": 0}
    processed_papers = 0

    summaries_by_doi: dict[str, dict] = dict(existing)

    for record in _iter_manifest(manifest_path):
        if paper_limit is not None and processed_papers >= paper_limit:
            break

        doi = str(record.get("doi", "")).strip()
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
            if input_source == "pdf":
                # pdf_path is known to be present because missing PDFs were
                # skipped above. This branch is the only place live direct-PDF
                # calls happen, which keeps batch/dev runs safely text-only.
                result = generate_summary_from_pdf(provider, pdf_path, prompt_template=prompt_template)
            else:
                result = generate_summary(provider, article_text, prompt_template=prompt_template)

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
    args = parser.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

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

    if not confirm_real_batch(profile, force=args.force):
        return 1

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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
