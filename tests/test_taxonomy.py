"""
Tests for src/scenarios/taxonomy.py — the versioned veterinary taxonomy.

All offline: pure dataclass construction and lookups, no filesystem or API
access.
"""

from __future__ import annotations

from scenarios.taxonomy import (
    TaxonomyCategory,
    TaxonomyInfo,
    TaxonomyTask,
    VET_TAXONOMY_V1,
    VeterinaryTaxonomy,
)


def test_taxonomy_is_versioned_and_citable() -> None:
    assert VET_TAXONOMY_V1.taxonomy_id == "vet_taxonomy_v1"
    assert VET_TAXONOMY_V1.version
    assert VET_TAXONOMY_V1.citation


def test_summarization_task_is_registered_with_full_taxonomy_info() -> None:
    task = VET_TAXONOMY_V1.find_task("veterinary_summary_quality")
    assert task is not None
    assert task.info.task
    assert task.info.what
    assert task.info.who
    assert task.info.when
    assert task.info.language == "English"


def test_summarization_task_belongs_to_research_literature_category() -> None:
    task = VET_TAXONOMY_V1.find_task("veterinary_summary_quality")
    category = VET_TAXONOMY_V1.find_category(task)
    assert category is not None
    assert category.key == "research_literature_summarization"


def test_describe_returns_flat_json_serializable_record() -> None:
    record = VET_TAXONOMY_V1.describe("veterinary_summary_quality")
    assert record == {
        "taxonomy_id": "vet_taxonomy_v1",
        "taxonomy_version": "1.0",
        "category_key": "research_literature_summarization",
        "category_display_name": "Research Literature Summarization",
        "task_key": "veterinary_summary_quality",
        "task_display_name": "Veterinary Summary Quality",
        "task_info": {
            "task": "Literature summarization",
            "what": (
                "Peer-reviewed veterinary research articles "
                "(cleaned text or direct PDF input)"
            ),
            "who": "Veterinary clinicians and researchers",
            "when": "2023-2026 publications",
            "language": "English",
        },
    }


def test_describe_returns_none_for_unregistered_scenario() -> None:
    assert VET_TAXONOMY_V1.describe("not_a_real_scenario") is None


def test_find_task_returns_none_for_unregistered_scenario() -> None:
    assert VET_TAXONOMY_V1.find_task("not_a_real_scenario") is None


def test_scaffold_categories_are_declared_with_no_tasks() -> None:
    scaffold_keys = {"clinical_decision_support", "client_communication"}
    scaffold_categories = [c for c in VET_TAXONOMY_V1.categories if c.key in scaffold_keys]
    assert len(scaffold_categories) == len(scaffold_keys)
    for category in scaffold_categories:
        assert category.tasks == ()
        assert category.display_name
        assert category.description


def test_taxonomy_categories_have_unique_keys() -> None:
    keys = [c.key for c in VET_TAXONOMY_V1.categories]
    assert len(keys) == len(set(keys))


def test_custom_taxonomy_lookup_roundtrip() -> None:
    """A hand-built taxonomy behaves the same as the module-level default,
    proving the lookup logic is generic rather than hardcoded to
    VET_TAXONOMY_V1's specific contents."""
    info = TaxonomyInfo(task="t", what="w", who="wh", when="whn", language="English")
    task = TaxonomyTask(key="k", display_name="K", scenario_name="scenario_x", info=info)
    category = TaxonomyCategory(key="cat", display_name="Cat", description="d", tasks=(task,))
    taxonomy = VeterinaryTaxonomy(
        taxonomy_id="test_taxonomy", version="0.1", citation="test", categories=(category,)
    )

    assert taxonomy.find_task("scenario_x") is task
    assert taxonomy.find_category(task) is category
    assert taxonomy.describe("scenario_x")["task_key"] == "k"
    assert taxonomy.describe("missing") is None
