"""
tests/manual_test_phase2.py — Phase 2 Success Checklist
=========================================================

HOW TO RUN
-----------
From the repository root (with your virtual environment active):

    python tests/manual_test_phase2.py

Or via pytest (which discovers functions named test_*):

    python -m pytest tests/manual_test_phase2.py -v

WHY A SEPARATE TEST FILE FOR PHASE 2?
---------------------------------------
Unit tests in tests/ serve two audiences:

  1. The researcher  — who needs a simple checklist to confirm the pipeline
                       works before spending any money on a real run.
  2. Future engineers — who need a regression suite that catches regressions
                        if any module is modified.

These three tests specifically cover the three most dangerous failure modes
in Phase 2:

  - Penny Test  : Confirms the budget guard actually kills the process.
                  Without this, a bug could loop indefinitely and spend real money.

  - Crash Test  : Confirms errors are written to the ledger even when things
                  go wrong.  Without this, a crashed run would be invisible.

  - Clean Test  : Confirms the Surgeon extracts useful text (Methods/Results)
                  rather than wasting the LLM's context window on bibliography.

WHAT IS sys.exit() AND WHY DO WE CATCH SystemExit HERE?
---------------------------------------------------------
BudgetGuard calls sys.exit(1) when spending exceeds the hard stop.
In normal pipeline execution, that kills the whole process — which is the
intended behaviour.

In a test, we WANT the budget guard to fire, but we don't want the test
process itself to die.  So we wrap the call in a try/except SystemExit block.
SystemExit is a subclass of BaseException (not Exception), so it's only caught
when you name it explicitly — a deliberate Python design choice that makes
"kill the process" different from "raise a runtime error".
"""

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the src/ directory is on sys.path so we can import pipeline modules.
# Why do this here instead of installing the package?
# Because researchers running a script directly from their IDE don't always
# have the package installed in editable mode.  This two-line shim is simpler
# than requiring `pip install -e .` before running tests.
# ---------------------------------------------------------------------------
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# Force dry-run mode for ALL tests, regardless of the local .env file.
# This guarantees the tests never make real API calls or charge real money,
# even if someone accidentally runs them with a live .env.
os.environ["DRY_RUN"] = "true"
os.environ["BUDGET_HARD_STOP"] = "0.00"

# Now import pipeline modules (after sys.path and env are configured above).
from utils import BudgetGuard, log_error, ERROR_LOG_PATH
from extract import remove_references_section, truncate_to_limit

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

FIXTURE_PATH  = Path(__file__).resolve().parent / "fixtures" / "sample_paper.json"
TEST_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ===========================================================================
# TEST 1 — The Penny Test
# ===========================================================================

def test_penny_test() -> None:
    """
    PENNY TEST: Assert the pipeline exits immediately if spending exceeds budget.

    Scenario: A researcher sets BUDGET_HARD_STOP=0.05 (five cents).
    If any single operation costs $0.06, the guard must fire and kill the process.

    WHY THIS MATTERS:
        Without a reliable hard stop, a looping bug or a misconfigured batch
        size could turn a $5 test run into a $500 accident overnight.

    PASS CRITERIA:
        BudgetGuard raises SystemExit when total_spent > hard_stop.
    """
    print("\n" + "=" * 60)
    print("TEST 1: Penny Test (BudgetGuard hard stop)")
    print("=" * 60)

    # Create a guard with a tiny limit to make it easy to exceed.
    guard = BudgetGuard(hard_stop=0.05)

    # Add a cost that brings us just under the limit — should NOT exit.
    guard.add_cost(0.04)
    assert guard.total_spent == 0.04, (
        f"Expected $0.04 spent, got ${guard.total_spent:.4f}"
    )
    print(f"  After $0.04: total_spent=${guard.total_spent:.4f}  [OK - below limit]")

    # Now add another cost that pushes us over the limit.
    try:
        guard.add_cost(0.02)  # 0.04 + 0.02 = 0.06, which exceeds 0.05
        # If we reach this line, the guard FAILED to exit — that's a test failure.
        assert False, (
            "FAIL: BudgetGuard did NOT call sys.exit() when spending exceeded the limit. "
            "The hard stop is broken — real money could be lost."
        )
    except SystemExit as exc:
        # Catching SystemExit is the EXPECTED outcome.
        print(f"  After $0.06: SystemExit raised with code {exc.code}  [OK - guard fired]")
        assert exc.code == 1, (
            f"Expected exit code 1 (abnormal termination), got {exc.code}"
        )

    print("  PENNY TEST: PASSED")


# ===========================================================================
# TEST 2 — The Crash Test
# ===========================================================================

def test_crash_test() -> None:
    """
    CRASH TEST: Assert errors are correctly written to the JSONL ledger.

    Scenario: A download fails for a paper.  log_error() must write a structured
    JSON entry to data/error_log.jsonl so the researcher can identify and retry
    failed papers without re-running the whole pipeline.

    WHY THIS MATTERS:
        The error ledger is the only record of what went wrong during a run.
        If log_error() silently fails or writes malformed JSON, the researcher
        has no way to know which papers are missing from the corpus.

    PASS CRITERIA:
        - The error log file exists after log_error() is called.
        - The last entry in the log is valid JSON.
        - The entry contains all four required fields: timestamp, doi, stage, message.
        - The field values match what was passed to log_error().
    """
    print("\n" + "=" * 60)
    print("TEST 2: Crash Test (error ledger correctness)")
    print("=" * 60)

    test_doi     = "10.1111/test.crash-test-99999"
    test_stage   = "download"
    test_message = "No OA version available"

    # Ensure the data directory exists for the error log.
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Call the real log_error() function — this writes to the actual ledger.
    log_error(test_doi, test_stage, test_message)

    # Verify the file now exists.
    assert ERROR_LOG_PATH.exists(), (
        f"FAIL: Error log file was not created at {ERROR_LOG_PATH}.\n"
        f"log_error() must create the file if it does not exist."
    )
    print(f"  Error log exists at {ERROR_LOG_PATH}  [OK]")

    # Read the last line of the log (the entry we just wrote).
    with open(ERROR_LOG_PATH, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    assert lines, "FAIL: Error log is empty after log_error() was called."
    last_line = lines[-1]

    # Verify the entry is valid JSON.
    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError as exc:
        assert False, (
            f"FAIL: Last line of error log is not valid JSON.\n"
            f"  Line   : {last_line}\n"
            f"  Error  : {exc}\n"
            f"  Cause  : log_error() may be writing plain text instead of JSON."
        )

    print(f"  Last entry is valid JSON  [OK]")

    # Verify all required fields are present and correct.
    required_fields = {
        "timestamp": None,          # Any non-empty string is acceptable.
        "doi":       test_doi,
        "stage":     test_stage,
        "message":   test_message,
    }

    for field, expected_value in required_fields.items():
        assert field in entry, (
            f"FAIL: Required field '{field}' is missing from error log entry.\n"
            f"  Entry: {entry}"
        )
        if expected_value is not None:
            assert entry[field] == expected_value, (
                f"FAIL: Field '{field}' mismatch.\n"
                f"  Expected : {expected_value}\n"
                f"  Got      : {entry[field]}"
            )

    print(f"  All required fields present and correct  [OK]")
    print(f"  Entry: {json.dumps(entry, indent=4)}")
    print("  CRASH TEST: PASSED")


# ===========================================================================
# TEST 3 — The Clean Test
# ===========================================================================

def test_clean_test() -> None:
    """
    CLEAN TEST: Assert references are stripped from text and truncation preserves content.

    Scenario: extract.py receives a paper's raw text that contains a References
    section at the end (as all real papers do).  After processing:
      - The word "References" (as a section heading) must not appear in the output.
      - The scientific content (Introduction, Methods, Results, Discussion) must
        be present in the output.
      - The output must be <= MAX_CHARS characters.

    WHY THIS MATTERS:
        If the Surgeon fails to remove references, the LLM summariser wastes
        20-30% of its context window on bibliography — a list of citations with
        zero summarisation value.  This test catches that regression.

    PASS CRITERIA:
        - Output does NOT contain the string "\nReferences\n" (heading pattern).
        - Output DOES start with "Introduction" (scientific content preserved).
        - Output length <= 12,000 characters.
    """
    print("\n" + "=" * 60)
    print("TEST 3: Clean Test (reference stripping and truncation)")
    print("=" * 60)

    # Load the fixture to get the raw text snippet.
    assert FIXTURE_PATH.exists(), (
        f"FAIL: Fixture file not found at {FIXTURE_PATH}.\n"
        f"The fixture is required for the Clean Test.\n"
        f"Expected location: tests/fixtures/sample_paper.json"
    )

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    raw_text = fixture.get("full_text_snippet", "")
    assert raw_text, "FAIL: Fixture 'full_text_snippet' is empty."

    print(f"  Raw text loaded: {len(raw_text):,} chars  [OK]")

    # Verify the raw fixture DOES contain a References section.
    # If it doesn't, the test is meaningless (we're not testing anything real).
    assert "References" in raw_text, (
        "FAIL: The fixture text does not contain a 'References' section.\n"
        "The fixture must include a References heading so we can test that it is removed."
    )
    print("  Fixture contains 'References' section  [OK - raw text is correct]")

    # --- Apply the cleaning pipeline (same logic as extract.py) ---
    text_no_refs = remove_references_section(raw_text)
    text_final   = truncate_to_limit(text_no_refs)

    # --- Assert 1: References heading must be gone ---
    # We check for the newline-preceded pattern to avoid false positives from
    # mid-sentence uses of the word "references".
    import re
    ref_pattern = re.compile(
        r"\n\s*(?:references?|bibliography|works\s+cited|literature\s+cited)\s*(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    assert not ref_pattern.search(text_final), (
        "FAIL: The word 'References' (as a section heading) still appears in the "
        "cleaned output.\n"
        "remove_references_section() did not strip the bibliography.\n"
        f"Output (last 500 chars): ...{text_final[-500:]}"
    )
    print("  'References' section heading NOT in cleaned output  [OK]")

    # --- Assert 2: Scientific content must be preserved ---
    assert text_final.startswith("Introduction"), (
        f"FAIL: Cleaned text does not start with 'Introduction'.\n"
        f"  Starts with: {text_final[:80]!r}\n"
        f"  The Surgeon may have cut too much or the fixture may be malformed."
    )
    print("  Cleaned text starts with 'Introduction'  [OK]")

    # --- Assert 3: Output is within the character limit ---
    assert len(text_final) <= 12_000, (
        f"FAIL: Cleaned text is {len(text_final):,} chars, "
        f"which exceeds the 12,000-char limit.\n"
        f"truncate_to_limit() did not reduce the text."
    )
    print(f"  Output length: {len(text_final):,} chars (<= 12,000)  [OK]")

    # --- Assert 4: Numerical results are still present (Discussion not truncated) ---
    assert "0.94" in text_final or "Discussion" in text_final, (
        "FAIL: Key scientific content (Discussion section or AUC value) was removed.\n"
        "The truncation cut too aggressively and removed Results/Discussion content.\n"
        "Check that remove_references_section() runs BEFORE truncate_to_limit()."
    )
    print("  Key scientific content preserved in output  [OK]")

    print("  CLEAN TEST: PASSED")


# ===========================================================================
# Runner
# ===========================================================================

def run_all_tests() -> None:
    """
    Run all three Phase 2 verification tests and print a summary.

    Each test prints its own PASS/FAIL output.  If any test raises an
    unhandled exception (e.g. AssertionError), it is caught here so the
    remaining tests still run and the researcher gets a complete picture.
    """
    print("\n" + "#" * 60)
    print("#  Phase 2 Success Checklist")
    print("#" * 60)

    results: dict[str, str] = {}

    tests = [
        ("Penny Test",  test_penny_test),
        ("Crash Test",  test_crash_test),
        ("Clean Test",  test_clean_test),
    ]

    for name, func in tests:
        try:
            func()
            results[name] = "PASSED"
        except AssertionError as exc:
            results[name] = f"FAILED — {exc}"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"ERROR  — {type(exc).__name__}: {exc}"

    # Print summary table.
    print("\n" + "=" * 60)
    print("PHASE 2 CHECKLIST SUMMARY")
    print("=" * 60)
    all_passed = True
    for name, status in results.items():
        icon = "[PASS]" if status == "PASSED" else "[FAIL]"
        print(f"  {icon}  {name:<20} {status}")
        if status != "PASSED":
            all_passed = False

    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED - Phase 2 is ready.")
        print("Next step: set DRY_RUN=false in .env and run src/collect.py")
    else:
        print("ONE OR MORE TESTS FAILED - review output above before proceeding.")
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
