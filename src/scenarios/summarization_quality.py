"""
src/scenarios/summarization_quality.py - Veterinary summary quality scenario

WHY THIS MODULE EXISTS
----------------------
Phase 3 already builds MedHELM-style EvaluationInstance rows. This scenario
names that task and exposes its records without moving model calls, judge calls,
or cost-sensitive logic into src/.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Iterable

from scenarios.base import Scenario


class VeterinarySummaryQualityScenario(Scenario):
    """Benchmark scenario: judge LLM summaries of veterinary papers by strata."""

    name = "veterinary_summary_quality"
    description = (
        "Evaluate veterinary literature summaries by species, design, topic, "
        "journal, and input source."
    )

    def records(self) -> Iterable[Any]:
        """Yield existing Phase 3 evaluation instances without live API calls."""
        llm_sum_dir = Path(__file__).resolve().parents[2] / "llm-sum"
        if str(llm_sum_dir) not in sys.path:
            sys.path.insert(0, str(llm_sum_dir))

        from eval_instances import iter_evaluation_instances

        yield from iter_evaluation_instances(
            summaries_path=self.paths.summaries_path,
            manifest_path=self.paths.manifest_path,
            manual_manifest_path=self.paths.manual_manifest_path,
        )

    def metadata(self) -> dict[str, Any]:
        data = super().metadata()
        data.update({
            "stratification_fields": [
                "species",
                "study_design",
                "clinical_topic",
                "journal",
                "input_source",
            ],
            "uses_live_api": False,
        })
        return data
