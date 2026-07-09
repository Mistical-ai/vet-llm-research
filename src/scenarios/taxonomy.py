"""
src/scenarios/taxonomy.py - Versioned veterinary benchmark taxonomy

WHY THIS MODULE EXISTS
-----------------------
MedHELM organizes its scenarios as category -> task, and gives every task
"what / who / when / language" domain metadata (see HELM's
``benchmark/presentation/taxonomy_info.py`` and the ``run_groups`` entries in
``benchmark/static/schema_medhelm.yaml``). This module borrows that shape for
one purpose: a veterinary benchmark that can be cited by a stable name and
version (``vet_taxonomy_v1``) instead of re-described in prose every time it
comes up in a manifest, a report, or a paper.

Populated categories cover the scenario that exists today
(``VeterinarySummaryQualityScenario``). Unpopulated ("scaffold") categories are
declared now with no tasks so future scenarios (clinical decision support,
client communication) have a place to register without changing the
taxonomy's shape or bumping its version for that alone.

This module intentionally does not import ``scenarios.summarization_quality``
(or any other scenario module) to avoid a circular import -- scenarios link to
the taxonomy by ``scenario_name`` string, not by class reference.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaxonomyInfo:
    """Domain metadata for one task, mirroring MedHELM's ``TaxonomyInfo`` fields."""

    task: str
    what: str
    who: str
    when: str
    language: str


@dataclass(frozen=True)
class TaxonomyTask:
    """One benchmark task, linked to a ``Scenario`` by name (not by import)."""

    key: str
    display_name: str
    scenario_name: str
    info: TaxonomyInfo


@dataclass(frozen=True)
class TaxonomyCategory:
    """A category grouping zero or more tasks.

    An empty ``tasks`` tuple marks a scaffold category: reserved for a future
    scenario, declared now so the taxonomy's shape does not need to change
    when that scenario is added.
    """

    key: str
    display_name: str
    description: str
    tasks: tuple[TaxonomyTask, ...] = ()


@dataclass(frozen=True)
class VeterinaryTaxonomy:
    """A versioned, citable veterinary benchmark taxonomy."""

    taxonomy_id: str
    version: str
    citation: str
    categories: tuple[TaxonomyCategory, ...]

    def find_task(self, scenario_name: str) -> TaxonomyTask | None:
        """Return the task linked to a scenario name, or None if unregistered."""
        for category in self.categories:
            for task in category.tasks:
                if task.scenario_name == scenario_name:
                    return task
        return None

    def find_category(self, task: TaxonomyTask) -> TaxonomyCategory | None:
        """Return the category a task belongs to."""
        for category in self.categories:
            if task in category.tasks:
                return category
        return None

    def describe(self, scenario_name: str) -> dict[str, object] | None:
        """Return a flat, JSON-serializable taxonomy record for one scenario.

        Returns None for a scenario not yet registered in the taxonomy so
        callers (manifests, reports) can fall back gracefully instead of
        crashing on a lookup miss.
        """
        task = self.find_task(scenario_name)
        if task is None:
            return None
        category = self.find_category(task)
        return {
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.version,
            "category_key": category.key if category else None,
            "category_display_name": category.display_name if category else None,
            "task_key": task.key,
            "task_display_name": task.display_name,
            "task_info": {
                "task": task.info.task,
                "what": task.info.what,
                "who": task.info.who,
                "when": task.info.when,
                "language": task.info.language,
            },
        }


# ---------------------------------------------------------------------------
# vet_taxonomy_v1 — the current, citable veterinary benchmark taxonomy
# ---------------------------------------------------------------------------

VET_TAXONOMY_V1 = VeterinaryTaxonomy(
    taxonomy_id="vet_taxonomy_v1",
    version="1.0",
    citation=(
        "Veterinary LLM Benchmark Taxonomy v1.0 (OVC Pet Trust vet-llm-research "
        "project). Category/task structure and task/what/who/when/language "
        "fields adapted from Stanford MedHELM's taxonomy conventions; no HELM "
        "code is imported."
    ),
    categories=(
        TaxonomyCategory(
            key="research_literature_summarization",
            display_name="Research Literature Summarization",
            description=(
                "Summarizing peer-reviewed veterinary research articles for "
                "clinicians and researchers."
            ),
            tasks=(
                TaxonomyTask(
                    key="veterinary_summary_quality",
                    display_name="Veterinary Summary Quality",
                    scenario_name="veterinary_summary_quality",
                    info=TaxonomyInfo(
                        task="Literature summarization",
                        what=(
                            "Peer-reviewed veterinary research articles "
                            "(cleaned text or direct PDF input)"
                        ),
                        who="Veterinary clinicians and researchers",
                        when="2023-2026 publications",
                        language="English",
                    ),
                ),
            ),
        ),
        TaxonomyCategory(
            key="clinical_decision_support",
            display_name="Clinical Decision Support",
            description=(
                "Scaffold category: reserved for a future scenario evaluating "
                "diagnostic or treatment-recommendation quality. No task is "
                "registered here yet."
            ),
        ),
        TaxonomyCategory(
            key="client_communication",
            display_name="Client Communication",
            description=(
                "Scaffold category: reserved for a future scenario evaluating "
                "client-facing explanations of veterinary findings. No task is "
                "registered here yet."
            ),
        ),
    ),
)
