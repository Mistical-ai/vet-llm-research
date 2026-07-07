"""
src/evaluation/rubric_scoring.py - Lightweight rubric scoring
==============================================================

WHY THIS MODULE EXISTS
----------------------
MedHELM-style evaluation is strongest when the rubric version and output schema
are explicit. This module adds a small, deterministic scoring layer for local
pipeline checks without calling judge models or changing Phase 3's append-only
``data/evaluations.jsonl``.

Phase 3's blind MedHELM judge in ``llm-sum/evaluator.py`` remains the
authoritative study endpoint. This offline rubric is auxiliary only.

Why a narrow YAML reader instead of PyYAML? The project owns one controlled
rubric file, and avoiding a new dependency keeps this change small, auditable,
and easy to roll back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from file_paths import descriptive_stem, doi_to_slug


DIMENSIONS = (
    "factual_accuracy",
    "hallucination_risk",
    "clinical_relevance",
    "completeness",
)

RUBRIC_MIN_SCORE = 1
RUBRIC_MAX_SCORE = 5
DEFAULT_RUBRIC_PATH = Path("docs") / "rubrics" / "rubric_v1.yaml"
DEFAULT_OUTPUT_PATH = Path("data") / "rubric_scores.jsonl"
DEFAULT_SUMMARIES_PATH = Path("data") / "summaries.jsonl"
DEFAULT_PROCESSED_DIR = Path("data") / "processed"

SPECIES_TERMS = {
    "dog", "dogs", "canine", "cat", "cats", "feline", "horse", "horses",
    "equine", "cow", "cows", "bovine", "cattle", "bird", "avian",
}
CLINICAL_TERMS = {
    "clinical", "treatment", "diagnosis", "prognosis", "outcome", "risk",
    "therapy", "management", "disease", "signs", "survival", "mortality",
}
COMPLETENESS_TERMS = {
    "objective": ("objective", "aim", "purpose", "question"),
    "methods": ("method", "study", "retrospective", "prospective", "randomized"),
    "population": ("dogs", "cats", "canine", "feline", "horses", "sample", "cases"),
    "results": ("result", "found", "increased", "decreased", "associated"),
    "significance": ("clinical", "relevance", "suggests", "important", "practice"),
    "limitations": ("limit", "caution", "small sample", "retrospective"),
}


def _split_key_value(line: str) -> tuple[str, str]:
    """Split the controlled YAML ``key: value`` line format."""
    if ":" not in line:
        raise ValueError(f"Invalid rubric line: {line!r}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def load_rubric(path: Path = DEFAULT_RUBRIC_PATH) -> dict[str, Any]:
    """Load the controlled ``rubric_v1.yaml`` shape without adding PyYAML."""
    rubric: dict[str, Any] = {"score_range": {}, "dimensions": {}}
    section: str | None = None
    current_dimension: str | None = None
    in_anchors = False

    if not path.exists():
        raise FileNotFoundError(f"Rubric file not found: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent == 0:
            current_dimension = None
            in_anchors = False
            if line == "score_range:":
                section = "score_range"
                continue
            if line == "dimensions:":
                section = "dimensions"
                continue
            key, value = _split_key_value(line)
            rubric[key] = value
            section = None
            continue

        if section == "score_range" and indent == 2:
            key, value = _split_key_value(line)
            rubric["score_range"][key] = int(value)
            continue

        if section == "dimensions" and indent == 2 and line.endswith(":"):
            current_dimension = line[:-1]
            rubric["dimensions"][current_dimension] = {"anchors": {}}
            in_anchors = False
            continue

        if section == "dimensions" and current_dimension and indent == 4:
            if line == "anchors:":
                in_anchors = True
                continue
            key, value = _split_key_value(line)
            rubric["dimensions"][current_dimension][key] = value
            continue

        if section == "dimensions" and current_dimension and in_anchors and indent == 6:
            key, value = _split_key_value(line)
            rubric["dimensions"][current_dimension]["anchors"][int(key)] = value
            continue

        raise ValueError(f"Unsupported rubric YAML shape near line: {raw_line!r}")

    return rubric


def validate_rubric(rubric: Mapping[str, Any]) -> None:
    """Require exactly four dimensions and complete 1-5 anchors for each."""
    if rubric.get("version") != "rubric_v1":
        raise ValueError("Rubric version must be 'rubric_v1'.")

    score_range = rubric.get("score_range")
    if not isinstance(score_range, Mapping):
        raise ValueError("Rubric must define score_range.")
    if score_range.get("min") != RUBRIC_MIN_SCORE or score_range.get("max") != RUBRIC_MAX_SCORE:
        raise ValueError("Rubric score_range must be 1-5.")

    dimensions = rubric.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise ValueError("Rubric must define dimensions.")
    if tuple(dimensions.keys()) != DIMENSIONS:
        raise ValueError(f"Rubric dimensions must be exactly: {', '.join(DIMENSIONS)}.")

    expected_anchors = set(range(RUBRIC_MIN_SCORE, RUBRIC_MAX_SCORE + 1))
    for name in DIMENSIONS:
        spec = dimensions[name]
        if not isinstance(spec, Mapping) or not spec.get("description"):
            raise ValueError(f"Rubric dimension {name} must include a description.")
        anchors = spec.get("anchors")
        if not isinstance(anchors, Mapping) or set(anchors.keys()) != expected_anchors:
            raise ValueError(f"Rubric dimension {name} must include anchors 1-5.")


def _tokens(text: str) -> set[str]:
    """Normalize text into lowercase word tokens for cheap overlap checks."""
    return set(re.findall(r"[a-zA-Z][a-zA-Z-]+", text.lower()))


def _numbers(text: str) -> set[str]:
    """Extract numeric claims so unsupported numbers can lower confidence."""
    return set(re.findall(r"\b\d+(?:\.\d+)?%?\b", text))


def _score_factual_accuracy(model_output: str, source_text: str) -> dict[str, Any]:
    """Score support using text overlap and unsupported numeric claims."""
    if not model_output.strip():
        return {"score": 1, "rationale": "No model output was provided."}

    unsupported_numbers = _numbers(model_output) - _numbers(source_text)
    if len(unsupported_numbers) >= 2:
        return {"score": 2, "rationale": "Multiple numeric claims were not found in the source text."}
    if len(unsupported_numbers) == 1:
        return {"score": 3, "rationale": "One numeric claim was not found in the source text."}

    source_tokens = _tokens(source_text)
    output_tokens = _tokens(model_output)
    overlap = len(source_tokens & output_tokens) / max(1, len(output_tokens))
    if overlap >= 0.75:
        return {"score": 5, "rationale": "Most summary terms are grounded in the source text."}
    if overlap >= 0.45:
        return {"score": 4, "rationale": "Summary terms are mostly grounded in the source text."}
    return {"score": 3, "rationale": "Grounding is partial based on simple token overlap."}


def _score_hallucination_risk(model_output: str, source_text: str) -> dict[str, Any]:
    """Estimate unsupported-claim risk from numbers and low source overlap."""
    unsupported_numbers = _numbers(model_output) - _numbers(source_text)
    output_tokens = _tokens(model_output)
    overlap = len(_tokens(source_text) & output_tokens) / max(1, len(output_tokens))

    if len(unsupported_numbers) >= 2 or overlap < 0.25:
        return {"score": 2, "rationale": "Unsupported numbers or low source overlap increase hallucination risk."}
    if unsupported_numbers or overlap < 0.45:
        return {"score": 3, "rationale": "Some claims need source verification."}
    if overlap >= 0.75:
        return {"score": 5, "rationale": "No obvious unsupported numeric claims were detected."}
    return {"score": 4, "rationale": "Hallucination risk appears low by deterministic checks."}


def _score_clinical_relevance(model_output: str) -> dict[str, Any]:
    """Score whether species and clinical context appear in the summary."""
    tokens = _tokens(model_output)
    has_species = bool(tokens & SPECIES_TERMS)
    clinical_hits = len(tokens & CLINICAL_TERMS)

    if has_species and clinical_hits >= 2:
        return {"score": 5, "rationale": "Species and clinical context are clearly present."}
    if has_species and clinical_hits == 1:
        return {"score": 4, "rationale": "Species context is present with some clinical framing."}
    if has_species or clinical_hits >= 1:
        return {"score": 3, "rationale": "Clinical relevance is partial or species context is incomplete."}
    return {"score": 2, "rationale": "Little species-specific or clinical context was detected."}


def _score_completeness(model_output: str) -> dict[str, Any]:
    """Score coverage of common veterinary-paper summary elements."""
    text = model_output.lower()
    covered = [
        name
        for name, terms in COMPLETENESS_TERMS.items()
        if any(term in text for term in terms)
    ]
    count = len(covered)

    if count >= 6:
        return {"score": 5, "rationale": "All essential summary elements were detected."}
    if count >= 5:
        return {"score": 4, "rationale": "Most essential summary elements were detected."}
    if count >= 3:
        return {"score": 3, "rationale": "Some essential summary elements were detected."}
    if count >= 1:
        return {"score": 2, "rationale": "Only a few essential summary elements were detected."}
    return {"score": 1, "rationale": "Essential summary elements were not detected."}


def score_output(
    model_output: str,
    source_text: str,
    rubric_path: Path = DEFAULT_RUBRIC_PATH,
) -> dict[str, Any]:
    """Return a stable JSON-compatible rubric score dictionary."""
    rubric = load_rubric(rubric_path)
    validate_rubric(rubric)

    scores = {
        "factual_accuracy": _score_factual_accuracy(model_output, source_text),
        "hallucination_risk": _score_hallucination_risk(model_output, source_text),
        "clinical_relevance": _score_clinical_relevance(model_output),
        "completeness": _score_completeness(model_output),
    }
    overall = round(sum(item["score"] for item in scores.values()) / len(scores), 2)
    return {
        "rubric_version": rubric["version"],
        "scores": scores,
        "overall_score": overall,
    }


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield valid JSON objects from JSONL and skip malformed rows."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _read_processed_jsonl(path: Path) -> str | None:
    """Return the cleaned ``text`` field from one processed JSONL cache file."""
    if not path.exists():
        return None
    line = path.read_text(encoding="utf-8").strip()
    if not line:
        return None
    try:
        text = json.loads(line).get("text")
    except (json.JSONDecodeError, AttributeError):
        return None
    return text if isinstance(text, str) and text.strip() else None


def _find_processed_jsonl(record: Mapping[str, Any], processed_dir: Path) -> Path | None:
    """Locate a processed cache file using descriptive then legacy DOI naming."""
    if record.get("journal") and record.get("title"):
        preferred = processed_dir / f"{descriptive_stem(record)}.jsonl"
        if preferred.exists():
            return preferred

    doi = str(record.get("doi", "")).strip()
    if doi:
        legacy = processed_dir / f"{doi_to_slug(doi)}.jsonl"
        if legacy.exists():
            return legacy
    return None


def _reference_text_for(record: Mapping[str, Any], processed_dir: Path) -> str:
    """Prefer processed full text, then manifest abstract, then title.

    MedHELM jury scoring compares summaries to full reference text. Matching
    that precedence here keeps offline rubric checks closer to Phase 3 without
    importing ``llm-sum/prepare_texts.py`` into ``src/evaluation/``.
    """
    processed_path = _find_processed_jsonl(record, processed_dir)
    if processed_path is not None:
        processed_text = _read_processed_jsonl(processed_path)
        if processed_text:
            return processed_text

    abstract = str(record.get("abstract") or "").strip()
    if abstract:
        return abstract
    return str(record.get("title") or "").strip()


def _successful_summary_slots(summary_record: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    """Yield successful model summaries from the existing summaries.jsonl shape."""
    models = summary_record.get("models")
    if isinstance(models, Mapping):
        for summarizer, slot in models.items():
            if not isinstance(slot, Mapping):
                continue
            status = slot.get("status")
            summary = str(slot.get("summary") or "").strip()
            if summary and status in (None, "success"):
                yield str(summarizer), summary
        return

    summary = str(summary_record.get("summary") or "").strip()
    if summary:
        yield str(summary_record.get("summarizer") or "unknown"), summary


def build_rubric_score_rows(
    *,
    summaries_path: Path = DEFAULT_SUMMARIES_PATH,
    manifest_records: Iterable[Mapping[str, Any]],
    rubric_path: Path = DEFAULT_RUBRIC_PATH,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> list[dict[str, Any]]:
    """Build rubric rows from summaries plus processed/manifest reference text."""
    manifest_index = {
        str(record.get("doi", "")).strip(): record
        for record in manifest_records
        if str(record.get("doi", "")).strip()
    }
    rows: list[dict[str, Any]] = []

    for summary_record in _iter_jsonl(summaries_path):
        doi = str(summary_record.get("doi", "")).strip()
        if not doi or doi not in manifest_index:
            continue

        source_text = _reference_text_for(manifest_index[doi], processed_dir)
        if not source_text:
            continue

        for summarizer, summary in _successful_summary_slots(summary_record):
            score = score_output(summary, source_text, rubric_path)
            rows.append({
                "doi": doi,
                "summarizer": summarizer,
                **score,
            })

    return rows


def write_rubric_scores(
    *,
    summaries_path: Path = DEFAULT_SUMMARIES_PATH,
    manifest_records: Iterable[Mapping[str, Any]],
    rubric_path: Path = DEFAULT_RUBRIC_PATH,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> int:
    """Write rubric score rows as a separate JSONL snapshot and return count."""
    rows = build_rubric_score_rows(
        summaries_path=summaries_path,
        manifest_records=manifest_records,
        rubric_path=rubric_path,
        processed_dir=processed_dir,
    )
    if not rows:
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_path.replace(output_path)
    return len(rows)
