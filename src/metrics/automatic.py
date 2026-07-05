"""Lightweight deterministic automatic metrics for summaries."""

from __future__ import annotations

from collections import Counter
import re


TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
SECTION_PATTERNS = {
    "objective": re.compile(r"\b(objective|aim|purpose|research question)\b", re.I),
    "methods": re.compile(r"\b(method|study design|retrospective|prospective|randomi[sz]ed|trial)\b", re.I),
    "species_sample": re.compile(
        r"\b(dog|dogs|canine|cat|cats|feline|horse|horses|equine|cow|cattle|bovine|n\s*=\s*\d+|\d+\s+(dogs|cats|horses|cattle|animals|patients))\b",
        re.I,
    ),
    "results": re.compile(r"\b(result|finding|significant|p\s*[<=>]|increased|decreased|associated)\b", re.I),
    "clinical_significance": re.compile(
        r"\b(clinical|clinician|practice|treatment|diagnosis|prognosis|recommend)\b",
        re.I,
    ),
    "limitations": re.compile(r"\b(limit|limitation|small sample|bias|retrospective design|further research)\b", re.I),
}


def tokenize(text: str) -> list[str]:
    """Return lowercase word tokens for overlap metrics."""
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]


def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    if n <= 0 or len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def rouge_recall(reference_text: str, candidate_summary: str, n: int = 1) -> float:
    """Compute ROUGE-N recall with clipped n-gram counts."""
    ref_counts = Counter(_ngrams(tokenize(reference_text), n))
    cand_counts = Counter(_ngrams(tokenize(candidate_summary), n))
    if not ref_counts:
        return 0.0
    overlap = sum(min(count, cand_counts[gram]) for gram, count in ref_counts.items())
    return round(overlap / sum(ref_counts.values()), 4)


def _lcs_length(left: list[str], right: list[str]) -> int:
    """Return longest common subsequence length using a memory-light DP row."""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0]
        for j, other in enumerate(right, 1):
            current.append(previous[j - 1] + 1 if token == other else max(previous[j], current[-1]))
        previous = current
    return previous[-1]


def rouge_l_recall(reference_text: str, candidate_summary: str) -> float:
    """Compute a lightweight ROUGE-L recall without external packages."""
    ref_tokens = tokenize(reference_text)
    cand_tokens = tokenize(candidate_summary)
    if not ref_tokens:
        return 0.0
    return round(_lcs_length(ref_tokens, cand_tokens) / len(ref_tokens), 4)


def compression_ratio(reference_text: str, candidate_summary: str) -> float:
    """Return summary token count divided by source token count."""
    ref_count = len(tokenize(reference_text))
    if ref_count == 0:
        return 0.0
    return round(len(tokenize(candidate_summary)) / ref_count, 4)


def extractive_coverage(reference_text: str, candidate_summary: str) -> float:
    """Estimate how much of the summary wording appears in the source text."""
    ref_tokens = set(tokenize(reference_text))
    cand_tokens = tokenize(candidate_summary)
    if not cand_tokens:
        return 0.0
    covered = sum(1 for token in cand_tokens if token in ref_tokens)
    return round(covered / len(cand_tokens), 4)


def section_coverage(candidate_summary: str) -> dict[str, bool | int | float]:
    """Check whether the summary appears to cover required vet-paper sections."""
    hits = {name: bool(pattern.search(candidate_summary or "")) for name, pattern in SECTION_PATTERNS.items()}
    count = sum(1 for hit in hits.values() if hit)
    return {**hits, "covered_count": count, "coverage_ratio": round(count / len(SECTION_PATTERNS), 4)}


def calculate_automatic_metrics(
    reference_text: str,
    candidate_summary: str,
    *,
    reference_summary: str | None = None,
) -> dict[str, object]:
    """Return local secondary metrics for one evaluation instance."""
    rouge_reference = reference_summary or reference_text
    return {
        "compression_ratio": compression_ratio(reference_text, candidate_summary),
        "extractive_coverage": extractive_coverage(reference_text, candidate_summary),
        "section_coverage": section_coverage(candidate_summary),
        "rouge_1": rouge_recall(rouge_reference, candidate_summary, n=1),
        "rouge_2": rouge_recall(rouge_reference, candidate_summary, n=2),
        "rouge_l": rouge_l_recall(rouge_reference, candidate_summary),
    }
