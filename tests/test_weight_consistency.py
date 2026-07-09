"""
Tests that llm-sum/eval_config/medhelm_vet_summary.yaml's documentation-mirror
criterion weights never silently drift from evaluator.py's runtime defaults.

evaluator._DEFAULT_MEDHELM_CRITERION_WEIGHTS is the single source of truth used
to compute jury scores. The YAML file repeats the same numbers for human
readers only; nothing in the pipeline reads weights from it. Without this
guard, an edit to one copy could go unnoticed until scores looked wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

from evaluator import _DEFAULT_MEDHELM_CRITERION_WEIGHTS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "llm-sum" / "eval_config" / "medhelm_vet_summary.yaml"

_CRITERION_RE = re.compile(r"^  (\w+):$")
_WEIGHT_RE = re.compile(r"^    weight:\s*([\d.]+)\s*$")


def _parse_criteria_weights(path: Path) -> dict[str, float]:
    """Extract the ``criteria:`` block's per-criterion weights.

    A narrow, controlled-format reader (no PyYAML) matching the project's
    dependency-light convention already used in src/evaluation/rubric_scoring.py.
    """
    weights: dict[str, float] = {}
    in_criteria_section = False
    current_criterion: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        if not raw_line.startswith(" "):
            in_criteria_section = raw_line.rstrip() == "criteria:"
            current_criterion = None
            continue

        if not in_criteria_section:
            continue

        criterion_match = _CRITERION_RE.match(raw_line)
        if criterion_match:
            current_criterion = criterion_match.group(1)
            continue

        weight_match = _WEIGHT_RE.match(raw_line)
        if weight_match and current_criterion:
            weights[current_criterion] = float(weight_match.group(1))

    return weights


def test_yaml_mirror_matches_code_defaults() -> None:
    yaml_weights = _parse_criteria_weights(_CONFIG_PATH)
    assert yaml_weights == _DEFAULT_MEDHELM_CRITERION_WEIGHTS, (
        "llm-sum/eval_config/medhelm_vet_summary.yaml's criteria weights have "
        "drifted from evaluator._DEFAULT_MEDHELM_CRITERION_WEIGHTS. Update the "
        "YAML documentation mirror to match the runtime source of truth."
    )


def test_yaml_mirror_is_non_empty() -> None:
    # Guards against the regex silently matching nothing if the YAML shape
    # changes (e.g. re-indentation), which would make the equality check above
    # vacuously pass on two empty dicts.
    yaml_weights = _parse_criteria_weights(_CONFIG_PATH)
    assert len(yaml_weights) == len(_DEFAULT_MEDHELM_CRITERION_WEIGHTS) > 0
