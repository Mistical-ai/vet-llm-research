"""
llm-sum/pilot_human_review.py — Pilot human-validation export (dev pool, self-contained folders)
=================================================================================================

IN PLAIN ENGLISH
-----------------
This script builds a small "dress rehearsal" version of the real human-review
export. Instead of drawing from the whole study, it only uses the small batch
of articles already summarized in test/dev mode. Every time you run it, it
creates one more make-believe reviewer's folder — first ``human1``, then
``human2``, then ``human3``, and so on — each one a self-contained package
(article text, AI summary, guide, and a fillable spreadsheet) that a real
person could open and score without touching any code. A single setting (the
"overlap ratio") controls how many of the same articles get repeated for the
next reviewer, so you can see whether two people would score the same article
similarly.

WHY THIS MODULE EXISTS
-----------------------
``human_review.py`` exports the *real* blind validation packets for the study
(also incremental ``humanN/`` folders, sampled evenly across the study's 5
journals from the whole ``data/evaluations.jsonl`` corpus). Before running
that for real, it helps to *trial* the whole reviewer experience on yourself
— and maybe one or two other testers — using just the small dev-mode sample
already sitting in ``data/dev_summaries_jsonl/``. This module is that trial
harness.

WHAT IT DOES DIFFERENTLY FROM THE REAL EXPORT
----------------------------------------------
1. **Scoped to the dev pool.** Only articles whose summaries are in
   ``data/dev_summaries_jsonl/`` are eligible, so the pilot grows naturally as
   you run more ``summarize --mode dev`` / ``evaluate --mode dev`` rounds — no
   code change needed.
2. **Backfills instead of erroring on a thin journal.** The real export
   refuses to guess when a journal can't supply its exact quota
   (:class:`human_review.JournalQuotaError`); the pilot's dev pool is
   expected to be thin early on, so it instead fills the shortfall from
   other journals with a loud warning (``on_shortfall="backfill"``,
   see ``human_review.sample_articles_from_journal_quota``) so the dry run
   stays usable while the pool grows.
3. **Self-contained folders in the nested per-item layout.** Each ``humanN/``
   folder holds the reviewer guide, a ``packet.md`` navigation index, an
   ``.xlsx`` scoresheet (dropdowns, frozen header, version-labeled rows), and
   one ``item_NNN/`` subfolder per item — each holding ``article.pdf`` (the
   matched source PDF copied out of ``data/raw``, or ``article.md`` as a
   fallback when it can't be found) plus ``summary.md`` — all written by the
   shared ``human_review.write_review_folder``, so a tester needs no repo
   access and no manual file digging.

WHAT IT REUSES (no duplicated logic)
-------------------------------------
Sampling, text join, blind rendering, the un-blinding key, and the incremental
``humanN``/overlap-carry machinery all come from ``human_review.py`` — this
module used to own a private copy of the incremental-folder and overlap-carry
code, but that's now shared with the real export (see
``human_review.select_incremental_rows``) so the two can never drift. The
dev-pool DOI scoping comes from ``eval_instances.read_dois_from_dev_folder``
(the same reader ``run_phase3.py``'s dev loop uses); the PDF lookup is
``file_paths.resolve_existing_pdf_path``. This module only adds the dev-pool
scoping and backfill-on-shortfall policy on top.

THE BLIND PROTOCOL STILL HOLDS
-------------------------------
Reviewer-facing files carry only ``item_id`` + article title/text + one
candidate summary — never a summariser identity. The article *title* is shown
(it identifies the paper, not the model), which is blind-safe; the private
``unblinding_key_humanN.json`` mapping back to (doi, summariser, judge, LLM
scores) is written as a *sibling* of the ``humanN/`` folders, never inside one,
so handing a tester their folder can't leak identity.

OFFLINE ONLY — reads dev summaries, evaluations, cached text, and PDFs already
on disk; never calls a paid API.
"""

# This line is a Python setting that makes newer type-hint syntax (used further
# down, e.g. ``dict[str, Any] | None``) work on older Python versions too. It
# has no effect on what the program actually does.
from __future__ import annotations

# Pulls in shared project setup (things like the DATA_DIR path used below) that
# every llm-sum script needs; the `*` means "import everything it offers".
from _bootstrap import *  # noqa: F401,F403

# The block below is a series of "import" statements. Each one loads a
# ready-made toolbox of code — either from Python's own standard library or
# from another file in this project — so this file can reuse that code
# instead of rewriting it from scratch.
import argparse  # reads command-line flags like --seed 42 (see main() below)
import json  # reads/writes .json files (the private "unblinding key")
import os  # reads environment variables / .env settings
import sys  # talks to the running program itself (exit codes, command-line args)
from dataclasses import dataclass  # a shortcut for defining simple "data bundle" classes
from pathlib import Path  # represents file/folder paths in an OS-independent way

from eval_instances import read_dois_from_dev_folder  # noqa: E402  (shared dev-pool reader)
from eval_report import iter_evaluation_rows  # noqa: E402  (same row source as the real export)
from human_review import (  # noqa: E402  (single source of truth for sampling/render/keys)
    ARTICLE_COUNT_CHOICES,
    JournalQuotaError,
    _build_instance_lookup,
    _build_review_items,
    build_unblinding_key,
    load_previous_unblinding_key,
    next_human_number,
    prompt_article_count_choice,
    select_incremental_rows,
    unblinding_key_path_for,
    write_review_folder,
)
import human_review  # noqa: E402  (for its module-level text-source path constants)

# Fixed folder locations this file reads from / writes to, plus default
# settings. ``DATA_DIR`` comes from the `_bootstrap import *` above. The "/"
# between two paths is Python's shorthand for "join this folder with that
# subfolder name" — e.g. DEV_SUMMARIES_DIR just means "the dev_summaries_jsonl
# folder inside the project's data folder".
DEV_SUMMARIES_DIR = DATA_DIR / "dev_summaries_jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
RAW_DIR = DATA_DIR / "raw"
PILOT_REVIEW_DIR = DATA_DIR / "pilot_human_review"

DEFAULT_OVERLAP_RATIO = 0.6  # 60% of the previous tester's articles carry over by default
DEFAULT_SEED = 42  # the "random seed": using the same number always reproduces the same sample


# ---------------------------------------------------------------------------
# Export entry point
# ---------------------------------------------------------------------------

# A "dataclass" is a quick way to define a simple container that just holds
# named pieces of data together (like a labeled form) without needing to
# write out boilerplate code by hand. ``frozen=True`` means once one of these
# is created, its fields can't be changed afterwards.
# PilotExportResult is the "report card" handed back after exporting one
# humanN/ folder: which tester it was, where the folder ended up, how many
# items/PDFs it contains, and anything that went wrong along the way.
@dataclass(frozen=True)
class PilotExportResult:
    human_number: int  # which tester this was (1, 2, 3, ...)
    human_dir: Path  # folder that was written, e.g. data/pilot_human_review/human1
    unblinding_key_path: Path  # where the private answer key was saved
    sample_size: int  # how many articles were requested
    items_exported: int  # how many (article, AI) items actually ended up in the folder
    overlap_units: int  # how many articles were repeated from the previous tester
    pdfs_copied: int  # how many item_NNN/article.pdf files were successfully copied in
    pdfs_missing: list[str]  # DOIs (one entry per affected item) whose PDF could not be found
    skipped_rows: int  # sampled rows that couldn't be matched to source text


def export_pilot_human_review(
    *,
    sample_size: int | None = None,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    seed: int = DEFAULT_SEED,
    evaluations_path: Path | None = None,
    dev_summaries_dir: Path | None = None,
    raw_dir: Path | None = None,
    output_dir: Path | None = None,
) -> PilotExportResult | None:
    """Export ONE more pilot reviewer folder (``humanN/``) from the dev pool.

    In plain English: this is the main "do everything" function that the
    command-line tool (``main()`` below) calls. Step by step, it: (1) finds
    which articles are eligible, (2) finds their scores, (3) works out how
    many to sample, (4) picks this tester's items (with overlap carried from
    the previous tester, balanced per journal — see
    ``human_review.select_incremental_rows``), (5) fetches the actual
    article/summary text, (6) writes out the humanN/ folder and the private
    answer key, then reports back a summary of what happened.

    Returns the :class:`PilotExportResult`, or ``None`` when there is nothing to
    export (no eligible evaluated dev articles) — the CLI turns ``None`` into a
    non-zero exit, mirroring ``human_review.main``'s empty-export handling.
    May raise :class:`human_review.JournalQuotaError` if even the FIRST tester's
    dev pool can't supply the requested per-journal quota — ``main()`` catches
    it and prints a friendly message (later testers fall back to a backfill
    warning instead, see the module docstring).
    """
    # If the caller didn't supply a particular path/setting, fall back to the
    # project-standard default location defined near the top of this file.
    resolved_evaluations = evaluations_path if evaluations_path is not None else EVALUATIONS_PATH
    resolved_dev_dir = dev_summaries_dir if dev_summaries_dir is not None else DEV_SUMMARIES_DIR
    resolved_raw = raw_dir if raw_dir is not None else RAW_DIR
    resolved_output = output_dir if output_dir is not None else PILOT_REVIEW_DIR

    # --- Step 1: which articles are even allowed to be sampled? ---
    # In plain English: look inside data/dev_summaries_jsonl/ to see which
    # articles have already been through a "summarize --mode dev" run. Only
    # those are eligible for the pilot — if none exist yet, stop here and
    # tell the user what to run first.
    # 1. Scope to DOIs that have dev-mode summaries.
    eligible_dois = read_dois_from_dev_folder(resolved_dev_dir)
    if not eligible_dois:
        print(f"[pilot_human_review] No dev summaries found in {resolved_dev_dir}. "
              "Run 'summarize --mode dev' (then 'evaluate --mode dev') first.")
        return None

    # --- Step 2: of those eligible articles, which have actually been scored? ---
    # In plain English: read every row out of evaluations.jsonl (the file the
    # LLM judges write their scores to), then keep only the rows belonging to
    # an eligible dev article.
    # 2. Filter evaluation rows down to that pool.
    all_rows = list(iter_evaluation_rows(resolved_evaluations))
    dois_with_evals = {str(r.get("doi", "")).strip() for r in all_rows}
    eligible_rows = [r for r in all_rows if str(r.get("doi", "")).strip() in eligible_dois]

    # Prerequisite warning: summarized-but-not-yet-evaluated dev articles.
    # In plain English: an article can be summarized but not yet judged (if
    # 'evaluate --mode dev' hasn't been run on it). Those can't be sampled
    # yet, so print a friendly reminder naming them instead of silently
    # dropping them.
    not_yet_evaluated = sorted(eligible_dois - dois_with_evals)
    if not_yet_evaluated:
        print(f"[pilot_human_review] WARNING: {len(not_yet_evaluated)} dev article(s) are "
              "summarized but have no rows in evaluations.jsonl yet — run "
              "'evaluate --mode dev' to include them. Skipping for now: "
              + ", ".join(not_yet_evaluated))

    if not eligible_rows:
        print("[pilot_human_review] No evaluated dev articles to sample from. Run "
              "'evaluate --mode dev' so evaluations.jsonl has rows for the dev "
              f"summaries in {resolved_dev_dir}.")
        return None

    # --- Step 3: how many articles should this tester be given? ---
    # In plain English: if the caller didn't ask for a specific number, fall
    # back to the smallest of the 5/10/25 choices — the same default the
    # interactive prompt itself falls back to when there's no one to ask.
    # 3. Resolve the sample size.
    resolved_sample_size = (
        sample_size if sample_size and sample_size > 0 else ARTICLE_COUNT_CHOICES[0]
    )

    # --- Step 4: which tester number is this, and what do they carry over? ---
    # In plain English: check the output folder to see what the next humanN
    # number should be, load the previous tester's answer key (if any), and
    # use the shared incremental-carry logic to pick this tester's exact set
    # of items — a journal-balanced fraction carried over, backfilled from
    # other journals (with a warning) if a journal's dev pool is too thin.
    # 4. Figure out which tester this run is, and carry the overlap.
    human_number = next_human_number(resolved_output)
    previous_key = load_previous_unblinding_key(resolved_output, human_number)
    selected_rows, overlap_units = select_incremental_rows(
        eligible_rows,
        human_number=human_number,
        sample_size=resolved_sample_size,
        seed=seed,
        overlap_ratio=overlap_ratio,
        previous_key=previous_key,
        on_shortfall="backfill",
    )

    # --- Step 5: fetch the actual article text and AI summary for each pick ---
    # In plain English: so far we've only had score rows (numbers); now go
    # fetch the real article text and the real candidate summary text that
    # belong to each selected row, so there's something for a human to read.
    # 5. Join to reference/candidate text (same two sources evaluate can read).
    lookup = _build_instance_lookup(
        human_review.SUMMARIES_PATH,
        human_review.SUMMARIES_TXT_DIR,
        human_review.DEV_TESTS_SUMMARIES_TXT_DIR,
    )
    items, skipped_rows = _build_review_items(selected_rows, lookup)
    if skipped_rows:
        print(f"[pilot_human_review] WARNING: {len(skipped_rows)} sampled row(s) could not be "
              "matched back to source text and were excluded.")
    if not items:
        print("[pilot_human_review] Nothing to export after joining to source text — "
              "check that data/summaries.jsonl still holds the dev summaries.")
        return None

    # --- Step 6: actually write the humanN/ folder to disk ---
    # In plain English: this hands off to the same folder-writing code the
    # REAL export uses (in human_review.py) — the reviewer guide, the
    # packet.md index, the fillable .xlsx spreadsheet, one folder per item
    # with the article text + summary, and copies of the matching PDFs.
    # 6. Write the self-contained humanN/ folder (nested per-item layout: guide +
    # packet index + .xlsx scoresheet + one item_id/ folder per item + matched
    # PDFs). write_review_folder is the same code the real export uses, so the two
    # folder shapes can never drift.
    resolved_output.mkdir(parents=True, exist_ok=True)
    human_dir = resolved_output / f"human{human_number}"
    scoresheet_name = f"scoresheet_human{human_number}.xlsx"
    copied, missing = write_review_folder(
        items, human_dir,
        reviewer_id=human_number,
        scoresheet_filename=scoresheet_name,
        raw_dir=resolved_raw,
        lookup=lookup,
    )
    if missing:
        print(f"[pilot_human_review] WARNING: no source PDF found in {resolved_raw} for "
              f"{len(set(missing))} article(s): " + ", ".join(sorted(set(missing)))
              + ". Those item(s) fall back to article.md (the cached text the AI read).")

    # --- Step 7: write the private "answer key" (never shown to the tester) ---
    # In plain English: save a separate file recording which real article,
    # which AI, and what the LLM judges scored for each item_id — this is
    # what lets the researcher (not the tester) later work out who wrote
    # what. It's saved next to, not inside, the humanN/ folder.
    # 7. Write the private un-blinding key (sibling of the folder, never inside).
    key = build_unblinding_key(
        items, seed=seed, sample_size=resolved_sample_size, reviewer_count=1,
    )
    # Add a few pilot-specific facts to the shared key format (the overlap
    # ratio/count and which tester number this was), then save it as a
    # ".json" file — a plain-text format for storing structured data.
    key["human_number"] = human_number
    key["overlap_units"] = overlap_units
    key["overlap_ratio"] = overlap_ratio
    key["sample_unit"] = "articles"
    key_path = unblinding_key_path_for(resolved_output, human_number)
    key_path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")

    # Print a short human-readable summary to the terminal so whoever ran the
    # command can see what just happened, and a loud reminder not to leak the
    # private key file.
    print(f"[pilot_human_review] exported human{human_number} ({len(items)} item(s), "
          f"{overlap_units} article(s) carried from human{human_number - 1 if human_number > 1 else '-'}, "
          f"{len(copied)} PDF(s) copied) to {human_dir}")
    print(f"[pilot_human_review] private key -> {key_path} — DO NOT hand this to a tester.")

    # Package everything up into the PilotExportResult "report card" and hand
    # it back to whoever called this function (e.g. main(), or a test).
    return PilotExportResult(
        human_number=human_number,
        human_dir=human_dir,
        unblinding_key_path=key_path,
        sample_size=resolved_sample_size,
        items_exported=len(items),
        overlap_units=overlap_units,
        pdfs_copied=len(copied),
        pdfs_missing=missing,
        skipped_rows=len(skipped_rows),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# In plain English: this section is what runs when someone types the command
# in PowerShell, e.g. `python llm-sum/pilot_human_review.py --overlap-ratio 0.6`.
# It reads settings from three possible places — a typed --flag, a value in
# the .env settings file, or a plain default — and then calls
# export_pilot_human_review() with the result.

def _env_int(name: str, default: int) -> int:
    """Read a whole-number setting from the .env file, or fall back to default.

    In plain English: looks up an environment variable (a named setting, e.g.
    from the project's .env file) by name and converts it to a whole number.
    If it's missing, blank, or not actually a number, quietly use the
    supplied default instead (printing a warning in the not-a-number case).
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[pilot_human_review] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def _env_float(name: str, default: float) -> float:
    """Same as :func:`_env_int` but for decimal-number settings (e.g. a ratio)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[pilot_human_review] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def _clamp_ratio(value: float) -> float:
    """Keep the overlap ratio in [0, 1] with a warning if it was out of range.

    In plain English: the overlap ratio only makes sense between 0 (nothing
    carried over) and 1 (everything carried over). If someone passes
    something outside that range, this quietly pulls it back into range and
    prints a warning, instead of letting a nonsensical value through.
    """
    if value < 0.0 or value > 1.0:
        clamped = max(0.0, min(1.0, value))
        print(f"[pilot_human_review] WARNING: overlap ratio {value} out of [0, 1]; "
              f"using {clamped}.")
        return clamped
    return value


def main(argv: list[str] | None = None) -> int:
    """The command-line entry point: reads flags/settings, then runs one export.

    In plain English: this is what actually executes when you type
    `python llm-sum/pilot_human_review.py ...` (or run it via run_phase3.py).
    It uses ``argparse`` — a standard tool for reading the ``--flag value``
    options typed after a command name — to define every option this script
    accepts, works out the final value for each (a typed flag always wins
    over an .env setting, which wins over a plain default), and then calls
    ``export_pilot_human_review()`` to do the actual work.
    """
    parser = argparse.ArgumentParser(
        description="Pilot human validation — export ONE more self-contained "
                    "reviewer folder (humanN/) from the dev-summary pool, with "
                    "matched PDFs and an .xlsx scoresheet.",
    )
    # Each `parser.add_argument(...)` call below defines one command-line
    # flag (e.g. --seed) that a user can type after the script name. The
    # `help=` text is what shows up if someone runs the script with --help.
    parser.add_argument("--sample-size", type=int, default=None,
                        help="Articles to sample (must be a multiple of the number "
                             "of study journals, e.g. 5/10/25). Default: "
                             "PILOT_HUMAN_REVIEW_SAMPLE_SIZE from .env, or an "
                             "interactive 5/10/25 prompt at a real terminal.")
    parser.add_argument("--overlap-ratio", type=float, default=None,
                        help="Fraction of the previous tester's articles to carry "
                             "over (default: PILOT_HUMAN_REVIEW_OVERLAP_RATIO, or "
                             "0.6). 1.0 = identical items every run, 0.0 = a fresh "
                             "draw each run.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Base sampling seed (default: PILOT_HUMAN_REVIEW_SEED "
                             "from .env, or 42).")
    parser.add_argument("--evaluations", type=Path, default=None,
                        help="Path to evaluations.jsonl (default: data/evaluations.jsonl).")
    parser.add_argument("--dev-summaries-dir", type=Path, default=None,
                        help="Dev-summary folder that scopes the pool "
                             "(default: data/dev_summaries_jsonl/).")
    parser.add_argument("--raw-dir", type=Path, default=None,
                        help="Where source PDFs are copied from (default: data/raw/).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Pilot output root (default: data/pilot_human_review/).")
    # This actually reads whatever the user typed on the command line and
    # matches it up against the flags defined above.
    args = parser.parse_args(argv)

    # For each setting, use the typed --flag if one was given; otherwise fall
    # back to reading it from the .env file; otherwise use the plain default.
    overlap_ratio = _clamp_ratio(
        args.overlap_ratio if args.overlap_ratio is not None
        else _env_float("PILOT_HUMAN_REVIEW_OVERLAP_RATIO", DEFAULT_OVERLAP_RATIO)
    )
    seed = args.seed if args.seed is not None else _env_int("PILOT_HUMAN_REVIEW_SEED", DEFAULT_SEED)

    # --- Work out the sample size, with a friendly fallback chain ---
    # In plain English: use the typed --sample-size if given; otherwise the
    # .env setting if given; otherwise, IF a real person is sitting at a
    # terminal (not a script/CI run), ask them to pick 5/10/25; otherwise
    # leave it at 0, which tells export_pilot_human_review() to fall back to
    # the smallest choice instead of getting stuck waiting for input that
    # will never come.
    # Sample size precedence: --sample-size > PILOT_HUMAN_REVIEW_SAMPLE_SIZE >
    # interactive 5/10/25 prompt (only at a real terminal) > 0.
    env_sample_size = _env_int("PILOT_HUMAN_REVIEW_SAMPLE_SIZE", 0)
    if args.sample_size is not None:
        sample_size = args.sample_size
    elif env_sample_size > 0:
        sample_size = env_sample_size
    elif sys.stdin.isatty():
        sample_size = prompt_article_count_choice(ARTICLE_COUNT_CHOICES[0])
    else:
        sample_size = 0

    # With every setting resolved, hand off to the main export function —
    # this is the line that actually does the work.
    try:
        result = export_pilot_human_review(
            sample_size=sample_size,
            overlap_ratio=overlap_ratio,
            seed=seed,
            evaluations_path=args.evaluations,
            dev_summaries_dir=args.dev_summaries_dir,
            raw_dir=args.raw_dir,
            output_dir=args.output_dir,
        )
    except JournalQuotaError as exc:
        # The dev pool can't supply this tester's per-journal quota even with
        # backfill (only reachable when the FIRST tester's fresh draw itself
        # comes up short) — report it plainly instead of a raw traceback.
        print(f"[pilot_human_review] {exc}")
        return 1
    # A command-line program signals success/failure with a number: 0 means
    # "everything worked", any non-zero number means "something went wrong".
    return 0 if result is not None else 1


# This is a standard Python pattern meaning "only run main() when this file
# is executed directly as a script (e.g. `python pilot_human_review.py`) —
# not when some other file merely imports pieces of it to reuse them."
# `sys.exit(...)` then reports main()'s 0/1 result back to PowerShell as the
# program's exit code.
if __name__ == "__main__":
    sys.exit(main())
