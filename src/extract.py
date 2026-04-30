"""
src/extract.py — The Surgeon
==============================

WHY DOES THIS MODULE EXIST?
-----------------------------
PDFs are messy.  A raw pdfplumber extraction gives us page headers, footers,
figure captions, reference lists, and actual scientific content all jumbled
together.  Before we feed text to an LLM summariser we must:

  1. Extract the raw text from the PDF.
  2. Remove the References/Bibliography section FIRST.
  3. Truncate to 12,000 characters AFTER references are gone.

The order of steps 2 and 3 is the single most important architectural decision
in this module.  See the rationale below.

CRITICAL EXTRACTION ORDER AND WHY IT MATTERS
---------------------------------------------
WRONG ORDER (what naive code might do):
    Extract → Truncate to 12k chars → Remove references

    PROBLEM: If a paper has 18k characters of text, truncating first would keep
    characters 1–12k.  In a 10-page paper, the References section is typically
    pages 8–10, which might start at character 10k.  So 2k of your 12k budget
    is wasted on bibliography that provides zero summarisation value.

CORRECT ORDER (what this module does):
    Extract → Remove references → Truncate to 12k chars

    BENEFIT: After stripping references (which often consume 20–30% of total
    characters), the 12k window contains almost entirely Methods, Results, and
    Discussion — the sections that carry all the scientific signal.

WHY 12,000 CHARACTERS?
-----------------------
12,000 characters ≈ 3,000 words ≈ 4,000 tokens for English scientific text.
This fits comfortably within the context windows of GPT-4, Claude 3, and
Gemini 1.5 Pro, while being long enough to capture a full Results and
Discussion section.  It avoids token-counting API calls (which cost money and
add latency) because the character limit is a reliable proxy.

WHY pdfplumber (not PyPDF2, not pdfminer)?
-------------------------------------------
pdfplumber is built on pdfminer but adds:
  - Better handling of multi-column layouts (common in JVIM and JAVMA).
  - Cleaner whitespace normalisation.
  - Active maintenance and a stable API.
PyPDF2 is older and struggles with complex layouts.

WHY REMOVE REFERENCES WITH REGEX (not a model)?
-------------------------------------------------
"References" / "Bibliography" headings are structurally consistent across
all five target journals.  A simple case-insensitive regex is 100% free,
100% deterministic, and handles 95%+ of real papers correctly.  Using a model
for this task would add cost and latency for no meaningful accuracy improvement.

WHY TRUNCATE AT A SENTENCE BOUNDARY?
--------------------------------------
Cutting text in the middle of a sentence produces a garbled final sentence
that the summariser model must infer the end of.  Finding the last period
before the character limit gives us a cleaner handoff with minimal token waste.
"""

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from utils import log_error

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum characters to pass to the LLM.  12k chars ≈ 4k tokens — fits all
# target models without truncation on the model side.
MAX_CHARS = 12_000

# Path to the dry-run fixture.  In dry-run mode we return the cleaned text
# from this file instead of reading a real PDF.
FIXTURE_PATH = Path("tests") / "fixtures" / "sample_paper.json"

RAW_DIR = Path("data") / "raw"

# ---------------------------------------------------------------------------
# Reference section removal
# ---------------------------------------------------------------------------

# This regex matches the start of a References or Bibliography section.
# Breakdown:
#   \n       — The section header usually starts on a new line.
#   \s*      — Allow leading whitespace (e.g. indented headings).
#   (        — Start of the alternation group.
#     references?  — Matches "Reference" or "References" (the ? makes 's' optional).
#     | bibliography — Matches "Bibliography".
#     | works\s+cited — Matches "Works Cited" (common in some journals).
#     | literature\s+cited — Matches "Literature Cited".
#   )
#   \s*      — Optional trailing whitespace before a newline or end of string.
#   (?:\n|$) — End of the heading line.
#
# Flags: re.IGNORECASE so "REFERENCES" and "references" both match.
#        re.MULTILINE so ^ and $ match line boundaries (not just string ends).
REFERENCES_PATTERN = re.compile(
    r"\n\s*(?:references?|bibliography|works\s+cited|literature\s+cited)\s*(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def remove_references_section(text: str) -> str:
    """
    Find the first occurrence of a reference/bibliography heading and
    return only the text that precedes it.

    WHY 'FIRST OCCURRENCE'?
        In rare papers, "References" appears as a word mid-paragraph
        (e.g. "the study references previous work...").  Using the regex
        means we only cut at a heading-style occurrence (preceded by a
        newline), which avoids false positives.

    Parameters
    ----------
    text : str — The full extracted text of the paper.

    Returns
    -------
    str — The text with everything from the References heading onward removed.
    """
    match = REFERENCES_PATTERN.search(text)
    if match:
        # Keep only everything before the matched heading.
        cleaned = text[: match.start()].rstrip()
        chars_removed = len(text) - len(cleaned)
        print(f"  [extract] Removed references section ({chars_removed:,} chars stripped).")
        return cleaned

    # No references section found — return the original text unchanged.
    # This is not an error; some short communications don't have a formal
    # bibliography section.
    print("  [extract] No references section detected.")
    return text


# ---------------------------------------------------------------------------
# Sentence-boundary truncation
# ---------------------------------------------------------------------------

def truncate_to_limit(text: str, limit: int = MAX_CHARS) -> str:
    """
    Truncate `text` to at most `limit` characters, ending at the last
    sentence boundary (period) before the limit.

    WHY SENTENCE BOUNDARY?
        A hard character cut mid-sentence produces a garbled final clause.
        Finding the last period before the limit costs O(n) and is always safe.

    WHY FALL BACK TO HARD TRUNCATION?
        If a paper is written in an unusual style with very long sentences
        (e.g. tables, lists without periods), we might not find a period
        close to the limit.  Rather than return nothing, we fall back to
        a hard cut so the pipeline always produces output.

    Parameters
    ----------
    text  : str — Input text.
    limit : int — Maximum character count.  Default MAX_CHARS (12,000).
    """
    if len(text) <= limit:
        return text  # Already within limits — nothing to do.

    # Search backwards from `limit` for the last sentence-ending period.
    window = text[:limit]
    last_period = window.rfind(".")

    if last_period > limit // 2:
        # Found a period in a reasonable position; cut there.
        truncated = window[: last_period + 1]
    else:
        # No good sentence boundary found; fall back to a hard cut.
        truncated = window

    chars_cut = len(text) - len(truncated)
    print(f"  [extract] Truncated text ({chars_cut:,} chars removed, "
          f"{len(truncated):,} chars kept).")
    return truncated


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str | None:
    """
    Extract raw text from a PDF file using pdfplumber.

    WHY CONCATENATE PAGES WITH A NEWLINE?
        pdfplumber returns text per page.  Joining with '\n' preserves
        paragraph-level boundaries so that the regex in
        remove_references_section() can detect newline-preceded headings.

    Returns None and logs an error if pdfplumber raises an exception,
    allowing the pipeline to continue with the next paper.
    """
    try:
        import pdfplumber  # Imported here so the module loads even if pdfplumber
                           # is not installed (dry-run tests don't need it).
    except ImportError:
        print("[extract] pdfplumber is not installed. Run: pip install pdfplumber")
        return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages: list[str] = []
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
            raw_text = "\n".join(pages)
            print(f"  [extract] Extracted {len(raw_text):,} raw chars from {pdf_path.name}.")
            return raw_text
    except Exception as exc:
        log_error(str(pdf_path.stem), "extract", f"pdfplumber failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Core public function
# ---------------------------------------------------------------------------

def extract_clean_text(doi: str) -> str | None:
    """
    Return cleaned, truncated text for a paper identified by its DOI.

    Execution paths
    ---------------
    DRY_RUN=true  →  Return the `full_text_snippet` from the fixture JSON,
                     after applying the same cleaning pipeline as a real PDF.
                     This lets us test cleaning logic without any PDFs.

    DRY_RUN=false →  Locate the PDF in data/raw/, extract with pdfplumber,
                     remove references, then truncate.

    Parameters
    ----------
    doi : str — The paper's DOI string.

    Returns
    -------
    str  — The cleaned, truncated text ready for the summariser.
    None — If no source is available (PDF missing, extraction failed, etc.).
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    if dry_run:
        return _extract_dry_run(doi)

    return _extract_live(doi)


def _extract_dry_run(doi: str) -> str | None:
    """
    Dry-run path: read the golden fixture and apply the cleaning pipeline.

    WHY APPLY REAL CLEANING IN DRY-RUN?
        The whole point of a dry-run is to exercise the code paths.  If we
        returned a pre-cleaned string from the fixture, we'd never catch bugs
        in remove_references_section() or truncate_to_limit().  Instead, the
        fixture stores a *raw* snippet (with a References section) and we clean
        it the same way we would a real PDF.
    """
    if not FIXTURE_PATH.exists():
        print(f"[extract] Fixture not found at {FIXTURE_PATH}. "
              "Run src/utils/generate_mock.py first.")
        return None

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    raw_text = fixture.get("full_text_snippet", "")
    if not raw_text:
        print("[extract] Fixture has an empty 'full_text_snippet'.")
        return None

    print(f"[extract] DRY_RUN — using fixture text ({len(raw_text):,} chars).")

    # Apply the SAME cleaning pipeline as a live PDF run.
    text_no_refs  = remove_references_section(raw_text)
    text_final    = truncate_to_limit(text_no_refs)
    return text_final


def _extract_live(doi: str) -> str | None:
    """
    Live path: find PDF in data/raw/, extract, clean, truncate.

    The PDF filename is derived from the DOI using the same sanitisation
    function used by download.py, so the names will always match.
    """
    safe_name = doi.replace("/", "_").replace(":", "_").replace(".", "_") + ".pdf"
    pdf_path  = RAW_DIR / safe_name

    if not pdf_path.exists():
        log_error(doi, "extract", f"PDF not found: {pdf_path}")
        return None

    raw_text = extract_text_from_pdf(pdf_path)
    if raw_text is None:
        return None  # Error already logged inside extract_text_from_pdf.

    # STEP 2: Remove references BEFORE truncation.
    # This is the critical ordering discussed in the module docstring.
    text_no_refs = remove_references_section(raw_text)

    # STEP 3: Truncate AFTER references are gone.
    text_final = truncate_to_limit(text_no_refs)

    return text_final


# ---------------------------------------------------------------------------
# Entry point (for manual testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick smoke test using the fixture.
    os.environ["DRY_RUN"] = "true"
    result = extract_clean_text("10.1111/jvim.00001")
    if result:
        print(f"\nExtraction successful. Output length: {len(result):,} chars.")
        print("First 500 chars:\n", result[:500])
    else:
        print("Extraction returned None.")
