"""
pipeline.py — Corpus Orchestration
=====================================

WHY DOES THIS MODULE EXIST?
-----------------------------
The pipeline currently has no single entry point that shows the researcher
the overall state of the corpus.  This module fills that gap: it merges OA-
downloaded PDFs with any manually supplied PDFs, deduplicates by DOI, and
prints a structured status report.

It does NOT replace the individual stage scripts:
  - python src/collect.py   → builds data/manifest.jsonl
  - python src/download.py  → downloads OA PDFs to data/raw/
  - python src/supplement.py → reports and guides manual supplementation
  - python pipeline.py      → merges sources and reports corpus health

This separation-of-concerns means each stage can be run independently,
re-run after a crash, or replaced with a different implementation without
touching the others.

SCENARIO LAYER (Phase 4)
-------------------------
Corpus policy (primary vs secondary PDFs, journal quotas, OA threshold) lives
in ``src/scenarios/`` so this file stays a thin CLI.  The default scenario is
``primary_research_corpus``.  Optional ``--use-rubric`` writes heuristic
offline scores to ``data/rubric_scores.jsonl`` — see ``docs/phase4/README.md``.
Phase 3's blind MedHELM judge in ``llm-sum/evaluator.py`` remains authoritative.

TWO-SOURCE CORPUS MODEL
------------------------
The final corpus is the union of:
  1. OA papers downloaded automatically by download.py
     → tracked in data/manifest.jsonl
     → PDFs in data/raw/

  2. Manually supplemented papers placed by the researcher
     → tracked in data/manual_manifest.jsonl (same schema as manifest.jsonl)
     → PDFs in data/raw/ (same naming convention as download.py uses)

pipeline.py merges both sources, deduplicates by DOI, validates that every
manual entry actually has a PDF in data/raw/, and reports the effective corpus
size (250 − total_missing).

WHY VALIDATE MANUAL PDFs BEFORE COUNTING THEM?
    data/manual_manifest.jsonl is edited by hand.  A researcher might add an
    entry before copying the PDF, or the filename might be mis-typed.  Counting
    an entry without a PDF would inflate the corpus size metric and hide the
    gap from downstream stages (extract.py, summarise.py).

SUCCESS THRESHOLD
------------------
OA_THRESHOLD = 200 means: if OA alone yields ≥ 200 PDFs, the run is
considered acceptable and the researcher can proceed with manual supplementation
to fill the remaining ≤ 50 papers.  Below 200, something is wrong (network
failure, wrong ISSNs, etc.) and manual supplementation alone is insufficient.

This threshold is set at 200 (80% of 250) rather than 250 because:
  - Not all papers are OA — some supplementation is expected.
  - Running extract.py and summarise.py on 200 papers still yields enough
    data for a meaningful Phase 4 analysis.
  - A threshold of 250 would require perfect OA coverage, which is unrealistic.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Add src/ to the Python path so pipeline.py (at workspace root) can import
# from src/collect.py, src/utils.py, etc. without an installed package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from file_paths import legacy_doi_filename  # noqa: E402
from scenarios import (  # noqa: E402
    PrimaryResearchCorpusScenario,
    ScenarioPaths,
    VeterinarySummaryQualityScenario,
)
from utils import env_path, log_error  # noqa: E402

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PATHS = ScenarioPaths()
DEFAULT_SCENARIO = PrimaryResearchCorpusScenario(DEFAULT_PATHS)

MANIFEST_PATH        = DEFAULT_PATHS.manifest_path
MANUAL_MANIFEST_PATH = DEFAULT_PATHS.manual_manifest_path
RAW_DIR              = DEFAULT_PATHS.raw_dir

# Total corpus target (5 journals × 50 papers each).
CORPUS_TARGET: int = DEFAULT_SCENARIO.corpus_target  # 250

# Minimum OA-sourced PDFs for the run to be considered acceptable without
# requiring extensive manual intervention.  See module docstring for rationale.
OA_THRESHOLD: int = DEFAULT_SCENARIO.oa_threshold


# ---------------------------------------------------------------------------
# Helper: DOI → filename (mirrors download.py exactly)
# ---------------------------------------------------------------------------

def _doi_to_filename(doi: str) -> str:
    """
    Return the legacy DOI-only filename for backward compatibility.
    """
    return legacy_doi_filename(doi)


# ---------------------------------------------------------------------------
# Corpus loader: merge OA + manual manifests
# ---------------------------------------------------------------------------

def _warn_invalid_manual(invalid_manual: list[dict]) -> None:
    """Print the existing manual-manifest warning after scenario validation."""
    if not invalid_manual:
        return

    print(
        f"\n[pipeline] WARNING: {len(invalid_manual)} manual manifest "
        f"entries have no PDF in data/raw/:"
    )
    for item in invalid_manual[:5]:
        print(f"  {item['doi']}: {item['reason']}")
    if len(invalid_manual) > 5:
        print(f"  ... and {len(invalid_manual) - 5} more")
    print(
        "  Add entries to data/manual_manifest.jsonl only AFTER placing "
        "PDFs in data/raw/."
    )


def load_corpus(scenario: PrimaryResearchCorpusScenario | None = None) -> dict:
    """
    Load and merge the OA manifest and optional manual manifest into a single
    deduplicated view of the corpus.

    MERGE LOGIC
    ------------
    1. Load every line from data/manifest.jsonl (OA records).
    2. Load every line from data/manual_manifest.jsonl (if it exists).
    3. Deduplicate by DOI — if the same DOI appears in both, the OA record
       takes precedence (manual record is silently skipped).
    4. For manual records only: validate that the PDF exists in data/raw/.
       Records without a confirmed PDF are reported as invalid and excluded
       from the downloaded count.

    WHY OA RECORD TAKES PRECEDENCE?
        The OA record was fetched automatically and has consistent metadata
        (covariates, abstract, etc.).  A manual record for the same DOI is
        likely a duplicate entry by the researcher; the OA version is more
        reliable.

    Returns
    -------
    dict with keys:
        records        : list[dict] — All deduplicated records (OA + manual).
        downloaded     : list[str]  — DOIs with PDFs confirmed in data/raw/.
        missing_pdfs   : list[str]  — DOIs in manifest but no PDF found.
        oa_count       : int        — Records loaded from data/manifest.jsonl.
        manual_count   : int        — Valid records from data/manual_manifest.jsonl.
        invalid_manual : list[dict] — Manual entries without confirmed PDFs.
    """
    active_scenario = scenario or DEFAULT_SCENARIO
    corpus = active_scenario.load_corpus(error_logger=log_error)
    _warn_invalid_manual(corpus["invalid_manual"])
    return corpus


# ---------------------------------------------------------------------------
# Status reporter
# ---------------------------------------------------------------------------

def report_corpus_status(
    corpus: dict,
    scenario: PrimaryResearchCorpusScenario | None = None,
) -> None:
    """
    Print a structured summary of the current corpus state.

    Shows per-journal breakdown against the 50-paper primary-research quota,
    total PDFs (primary + secondary), and whether the OA-only run meets the
    OA_THRESHOLD (200) for manual supplementation to be sufficient.

    WHAT IS A PRIMARY vs SECONDARY PDF?
        Primary  — an original research article (filename does NOT start with "2_").
                   These count toward the 50-paper quota.
        Secondary — a systematic review or meta-analysis (filename starts with "2_").
                   These are kept for reference but do NOT count toward the quota.

    Parameters
    ----------
    corpus : dict — Output of load_corpus().
    """
    active_scenario = scenario or DEFAULT_SCENARIO
    report = active_scenario.build_status_report(corpus)

    print("\n" + "=" * 68)
    print("  CORPUS STATUS")
    print("=" * 68)
    print(f"  Manifest entries (OA):             {report.oa_count:>5}")
    print(f"  Manifest entries (manual):         {report.manual_count:>5}")
    print(f"  Total unique DOIs:                 {len(report.records):>5}")
    print(f"  PDFs confirmed — primary:          {report.n_primary:>5}  (count toward quota)")
    print(f"  PDFs confirmed — secondary (2_):   {report.n_secondary:>5}  (reviews; not in quota)")
    print(f"  PDFs confirmed — total:            {report.n_total:>5}")
    print(f"  PDFs still missing:                {len(report.missing_pdfs):>5}")
    print(f"  Corpus target (primary only):      {active_scenario.corpus_target:>5}")
    print(
        f"  Primary quota progress:            {report.n_primary:>5}  "
        f"({report.n_primary}/{active_scenario.corpus_target})"
    )
    print()

    # --- Per-journal breakdown ---
    print(f"  {'Journal':<25} {'Target':>6}  {'Primary':>8}  {'Secondary':>10}  {'Status'}")
    print("  " + "-" * 70)
    for journal, target in active_scenario.journal_targets.items():
        counts    = report.journal_counts.get(journal, {"primary": 0, "secondary": 0})
        primary   = counts["primary"]
        secondary = counts["secondary"]
        if primary >= target:
            status = "✓ OK"
        else:
            need   = target - primary
            status = f"NEED {need} MORE PRIMARY"
            if secondary > 0:
                status += f" ({secondary} secondary available)"
        print(f"  {journal:<25} {target:>6}  {primary:>8}  {secondary:>10}  {status}")
    print("=" * 68)

    # --- Overall assessment (based on primary count only) ---
    if report.n_primary >= active_scenario.corpus_target:
        print(
            f"\n[pipeline] Corpus complete: "
            f"{report.n_primary}/{active_scenario.corpus_target} primary PDFs acquired."
        )

    elif report.n_primary >= active_scenario.oa_threshold:
        remaining = active_scenario.corpus_target - report.n_primary
        print(
            f"\n[pipeline] OA corpus acceptable: "
            f"{report.n_primary} >= {active_scenario.oa_threshold} threshold.\n"
            f"[pipeline] {remaining} primary papers still needed (250 target).\n"
            f"[pipeline] Manual supplementation steps:\n"
            f"  1. Run: python src/supplement.py  → generates data/missing_papers.csv\n"
            f"  2. Acquire PDFs and place in:     data/raw/\n"
            f"  3. Add entries to:                data/manual_manifest.jsonl\n"
            f"  4. Re-run: python pipeline.py     → verify updated count"
        )

    else:
        print(
            f"\n[pipeline] WARNING: Only "
            f"{report.n_primary}/{active_scenario.oa_threshold} minimum primary PDFs acquired.\n"
            f"[pipeline] Possible causes:\n"
            f"  - Network errors during download.py run\n"
            f"  - Low OA availability for the target journals in 2023-2026\n"
            f"  - manifest.jsonl is empty or has wrong journal names\n"
            f"[pipeline] Run: python src/supplement.py to diagnose per-journal gaps."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_pipeline_scenario(name: str) -> PrimaryResearchCorpusScenario:
    """Resolve the small set of scenarios that pipeline.py can run today."""
    if name == PrimaryResearchCorpusScenario.name:
        return PrimaryResearchCorpusScenario()
    raise ValueError(
        f"Unknown scenario {name!r}. "
        f"Supported scenarios: {PrimaryResearchCorpusScenario.name}"
    )


# Every scenario registered under src/scenarios/, for --list-scenarios only.
# Only PrimaryResearchCorpusScenario is runnable through --scenario above: its
# records() yields corpus rows this CLI's status report understands.
# VeterinarySummaryQualityScenario yields evaluation instances instead (see
# src/scenarios/summarization_quality.py) and is driven from
# llm-sum/run_phase3.py, not from this corpus-status flow. It is listed here
# so it is discoverable rather than silently unwired.
REGISTERED_SCENARIOS = (PrimaryResearchCorpusScenario, VeterinarySummaryQualityScenario)


def _print_scenario_catalog() -> None:
    """Print name/description/metadata for every registered scenario."""
    print("\n" + "=" * 68)
    print("  REGISTERED SCENARIOS")
    print("=" * 68)
    for scenario_cls in REGISTERED_SCENARIOS:
        meta = scenario_cls().metadata()
        print(f"\n[{meta['name']}]")
        print(f"  {meta['description']}")
        for key, value in meta.items():
            if key in ("name", "description"):
                continue
            print(f"  {key}: {value}")
    print(
        "\nOnly 'primary_research_corpus' is runnable via --scenario here; "
        "'veterinary_summary_quality' is evaluated through "
        "llm-sum/run_phase3.py evaluate.\n"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI flags while keeping the default command unchanged."""
    parser = argparse.ArgumentParser(description="Report veterinary corpus health.")
    parser.add_argument(
        "--scenario",
        default=PrimaryResearchCorpusScenario.name,
        choices=[PrimaryResearchCorpusScenario.name],
        help="Scenario to report; defaults to the current primary research corpus.",
    )
    parser.add_argument(
        "--use-rubric",
        action="store_true",
        help=(
            "Write lightweight rubric scores to data/rubric_scores.jsonl "
            "without changing corpus status output."
        ),
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Print name/description/metadata for every registered scenario and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the corpus status CLI and return a shell-friendly exit code."""
    args = build_arg_parser().parse_args(argv)

    if args.list_scenarios:
        _print_scenario_catalog()
        return 0

    scenario = resolve_pipeline_scenario(args.scenario)

    print("[pipeline] Loading corpus (OA manifest + manual manifest)...")
    corpus = load_corpus(scenario)
    report_corpus_status(corpus, scenario)

    if args.use_rubric:
        # Import lazily so default pipeline.py runs remain a corpus scoreboard,
        # and the rubric writer cannot affect normal exit-code behavior.
        from evaluation.rubric_scoring import write_rubric_scores

        output_path = env_path("RUBRIC_SCORES_PATH", "data/rubric_scores.jsonl")
        written = write_rubric_scores(
            summaries_path=env_path("SUMMARIES_JSONL_PATH", "data/summaries.jsonl"),
            manifest_records=corpus["records"],
            rubric_path=env_path("OFFLINE_RUBRIC_PATH", "docs/rubrics/rubric_v1.yaml"),
            output_path=output_path,
        )
        print(f"[pipeline] Rubric scores written: {written} row(s) -> {output_path}")

    # Exit with code 1 if primary-research PDFs are below the minimum threshold.
    # WHY sys.exit(1)?
    #   Makes pipeline.py usable in a CI/CD check or a shell script that
    #   should fail fast when the corpus is critically under-populated.
    # WHY CHECK PRIMARY ONLY?
    #   Secondary PDFs (reviews) are backup material — the corpus health is
    #   determined by how many primary research articles we have.
    n_primary = len(corpus["downloaded_primary"])
    if n_primary < scenario.oa_threshold:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
