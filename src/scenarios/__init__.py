"""
src/scenarios/ — Lightweight MedHELM-style scenario names
==========================================================

WHY THIS PACKAGE EXISTS
-----------------------
MedHELM benchmarks are organized as named scenarios: fixed inputs, fixed
selection rules, and reproducible metadata.  This package adds that naming
layer to the veterinary pipeline without importing the HELM framework or
moving paid API logic out of ``llm-sum/``.

Each scenario exposes:
  - ``name`` / ``description`` — stable identifiers for logs and tests
  - ``records()`` — the scenario's input rows (manifest rows or eval instances)
  - ``paths`` — filesystem locations, read from ``.env`` where applicable

``pipeline.py`` uses ``PrimaryResearchCorpusScenario`` today.  Phase 3 can
adopt ``VeterinarySummaryQualityScenario`` when evaluation orchestration moves
here; the class already wraps ``llm-sum/eval_instances.py`` without live calls.
"""

from scenarios.base import Scenario, ScenarioPaths
from scenarios.corpus_status import CorpusStatusReport, PrimaryResearchCorpusScenario
from scenarios.summarization_quality import VeterinarySummaryQualityScenario
from scenarios.taxonomy import (
    TaxonomyCategory,
    TaxonomyInfo,
    TaxonomyTask,
    VeterinaryTaxonomy,
    VET_TAXONOMY_V1,
)

__all__ = [
    "Scenario",
    "ScenarioPaths",
    "CorpusStatusReport",
    "PrimaryResearchCorpusScenario",
    "VeterinarySummaryQualityScenario",
    "TaxonomyCategory",
    "TaxonomyInfo",
    "TaxonomyTask",
    "VeterinaryTaxonomy",
    "VET_TAXONOMY_V1",
]
