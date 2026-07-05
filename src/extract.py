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

WHY pymupdf4llm (primary) + pdfplumber (fallback)?
--------------------------------------------------
pymupdf4llm wraps PyMuPDF (fitz) and converts PDFs to structured Markdown
in one call.  It uses spatial block analysis to detect columns and linearise
them in true reading order, converting section headings to ## headers and
tables to pipe-delimited Markdown rows.  This eliminates the whitespace-column
artifacts that pdfplumber produced for two-column journal layouts (JVIM, JAVMA).
pdfplumber is retained as a fallback for files that pymupdf4llm cannot open.

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

from file_paths import preferred_pdf_path
from utils import log_error
from validation.text import truncate_to_sentence_boundary

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Kept for backwards-compatibility — imported by older code and tests.
# prepare_texts.py no longer truncates at extraction time; this value is now
# only used if a caller explicitly passes it to truncate_to_limit().
MAX_CHARS = 12_000

# When True, strip the references section before caching. Set to false for
# debugging if you suspect the pattern is matching too early.
REMOVE_REFERENCES: bool = os.getenv("REMOVE_REFERENCES", "true").lower() == "true"

# When True, ask pdfplumber/pdfminer to preserve the PDF's text flow. This is
# important for two-column journals (JVIM, VRU), where default extraction can
# interleave left and right columns into scrambled sentences.
PDFPLUMBER_USE_TEXT_FLOW: bool = (
    os.getenv("PDFPLUMBER_USE_TEXT_FLOW", "true").lower() == "true"
)
_TEXT_FLOW_WARNING_SHOWN = False

# Path to the dry-run fixture.  In dry-run mode we return the cleaned text
# from this file instead of reading a real PDF.
FIXTURE_PATH = Path("tests") / "fixtures" / "sample_paper.json"

RAW_DIR = Path("data") / "raw"
MANIFEST_PATH = Path("data") / "manifest.jsonl"

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
    r"\n\s*(?:#{1,6}\s+)?(?:references?|reference\s+list|cited\s+references?|bibliography|works\s+cited|literature\s+cited)\s*(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Fallback for two-column PDFs where pdfplumber merges "REFERENCES" onto the
# same line as adjacent column text (e.g. ORCID block + "REFERENCES" + citation
# header all on one line). Fires only when the next line looks like a numbered
# citation (1. Author or [1] Author) to avoid false positives.
REFERENCES_INLINE_RE = re.compile(
    r"\n[^\n]{0,200}\bREFERENCES?\b[^\n]*\n(?=\s*(?:\d+[\.\):]|\[\d+\]))",
    re.IGNORECASE,
)

# Wiley Online Library embeds a per-page download watermark in extracted text.
# pdfplumber picks it up at every page boundary. Each instance has the form:
#   {ISSN}\n{YEAR}\n{ISSUE/VOL}\nDownloaded\nfrom\nhttps://onlinelibrary.wiley.com/...
#   ...\nCreative\nCommons\n[Attribution ]License
# The (?:\d[\d,]*\n){0,5} prefix captures the ISSN/year/issue lines before
# "Downloaded" so they are removed together with the rest of the block.
# {0,3000} prevents over-matching while covering messy multi-column layouts.
WILEY_WATERMARK_RE = re.compile(
    r"(?:\d[\d,]*\n){0,5}Downloaded[\s\S]{0,3000}?Creative\s+Commons\s+(?:Attribution[- ])?License",
    re.IGNORECASE,
)


def clean_publisher_noise(text: str) -> str:
    """
    Remove publisher-injected watermarks from PDF-extracted text.

    Currently handles Wiley Online Library download footers, which pdfplumber
    picks up at every page boundary in two-column journals (JVIM, VRU). In some
    papers these watermarks account for >60% of the extracted character count,
    making papers appear far shorter than they are.

    Must be called BEFORE remove_references_section() because watermarks can
    appear after the reference list on the final page — stripping references
    first would leave a trailing watermark block.
    """
    cleaned = WILEY_WATERMARK_RE.sub("", text)
    # Collapse any runs of 3+ blank lines created by watermark removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    removed = len(text) - len(cleaned)
    if removed > 0:
        print(f"  [extract] Removed publisher watermark(s) ({removed:,} chars stripped).")
    return cleaned


def remove_references_section(text: str, doi: str | None = None) -> str:
    """
    Find the LAST occurrence of a reference/bibliography heading and return
    only the text that precedes it.

    WHY LAST OCCURRENCE?
        Scientific papers sometimes mention "References" early — in a Methods
        section ("see References 3-5"), in a Table of Contents, or in a heading
        like "Cross-References". The ACTUAL bibliography section is always the
        final major section of a research article. Taking the last match is
        therefore far more robust than taking the first, which would discard
        everything from Methods/Results onward if an early match fires.

    The strict pattern requires the heading on its own line. The inline fallback
    (REFERENCES_INLINE_RE) catches two-column PDFs where pdfplumber merges the
    heading with adjacent column content onto the same line.

    Parameters
    ----------
    text : str — The full extracted text of the paper.

    Returns
    -------
    str — The text with everything from the last References heading onward removed.
    """
    matches = list(REFERENCES_PATTERN.finditer(text))
    used_inline_fallback = False
    if not matches:
        # Fallback: two-column layout may have merged "REFERENCES" mid-line.
        matches = list(REFERENCES_INLINE_RE.finditer(text))
        used_inline_fallback = bool(matches)

    if matches:
        # Use the LAST match — the bibliography is always the final section.
        match = matches[-1]
        cleaned = text[: match.start()].rstrip()
        chars_removed = len(text) - len(cleaned)
        if used_inline_fallback:
            label = doi or "unknown DOI"
            print(f"  [extract] used inline reference fallback for {label}.")
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
    truncated = truncate_to_sentence_boundary(text, limit)
    if truncated == text:
        return text  # Already within limits — nothing to do.

    chars_cut = len(text) - len(truncated)
    print(f"  [extract] Truncated text ({chars_cut:,} chars removed, "
          f"{len(truncated):,} chars kept).")
    return truncated


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str | None:
    """
    Extract text from a PDF file, preferring pymupdf4llm (Markdown output)
    with pdfplumber as a fallback for files that pymupdf cannot open.
    """
    text = _extract_with_pymupdf4llm(pdf_path)
    if text:
        return text
    return _extract_with_pdfplumber(pdf_path)


def _extract_with_pymupdf4llm(pdf_path: Path) -> str | None:
    """
    Convert a PDF to structured Markdown using pymupdf4llm.

    pymupdf4llm uses spatial block analysis to linearise multi-column layouts
    in true reading order and converts tables to pipe-delimited Markdown rows,
    eliminating the whitespace-column artifacts produced by pdfplumber for
    two-column journals (JVIM, JAVMA, JFMS).

    Returns None (silently) when the package is not installed, so the caller
    falls back to pdfplumber without user-visible noise.
    """
    try:
        import pymupdf4llm
    except ImportError:
        return None
    try:
        md = pymupdf4llm.to_markdown(str(pdf_path))
        if md and md.strip():
            print(f"  [extract] pymupdf4llm: {len(md):,} chars from {pdf_path.name}.")
            return md
        return None
    except Exception as exc:
        print(f"  [extract] pymupdf4llm failed ({exc}); trying pdfplumber.")
        return None


def _extract_with_pdfplumber(pdf_path: Path) -> str | None:
    """
    Fallback extractor using pdfplumber page-by-page text extraction.

    WHY CONCATENATE PAGES WITH A NEWLINE?
        pdfplumber returns text per page.  Joining with '\n' preserves
        paragraph-level boundaries so that the regex in
        remove_references_section() can detect newline-preceded headings.
    """
    try:
        import pdfplumber
    except ImportError:
        print("[extract] pdfplumber is not installed. Run: pip install pdfplumber")
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages: list[str] = []
            for page in pdf.pages:
                page_text = _extract_page_text(page)
                if page_text:
                    pages.append(page_text)
            raw_text = "\n".join(pages)
            print(f"  [extract] pdfplumber fallback: {len(raw_text):,} chars from {pdf_path.name}.")
            return raw_text
    except Exception as exc:
        log_error(str(pdf_path.stem), "extract", f"pdfplumber failed: {exc}")
        return None


def _extract_page_text(page) -> str | None:
    """
    Extract one PDF page while preserving reading order in two-column layouts.

    Preferred path uses pdfplumber/pdfminer's text-flow mode. Older pdfplumber
    versions may not accept ``use_text_flow``; when that happens we warn once
    and use tighter character-based tolerances instead.
    """
    if PDFPLUMBER_USE_TEXT_FLOW:
        try:
            text = page.extract_text(layout=True, use_text_flow=True)
        except TypeError:
            _warn_text_flow_not_supported()
            return page.extract_text(x_tolerance=3, y_tolerance=3) or page.extract_text()
        if text:
            return text
        return page.extract_text()

    return page.extract_text(x_tolerance=3, y_tolerance=3) or page.extract_text()


def _warn_text_flow_not_supported() -> None:
    """Print the text-flow compatibility warning once per process."""
    global _TEXT_FLOW_WARNING_SHOWN
    if _TEXT_FLOW_WARNING_SHOWN:
        return
    print(
        "  [extract] WARNING: PDFPLUMBER_USE_TEXT_FLOW=true but this "
        "pdfplumber version does not support use_text_flow; falling back to "
        "x_tolerance=3, y_tolerance=3."
    )
    _TEXT_FLOW_WARNING_SHOWN = True


# ---------------------------------------------------------------------------
# Core public function
# ---------------------------------------------------------------------------

def _manifest_record_for_doi(doi: str) -> dict:
    """
    Return manifest metadata for a DOI when available.

    Descriptive filenames include journal and title, so extraction needs the
    same metadata-aware path resolver as download.py.  If the manifest is not
    available, we fall back to DOI-only lookup for older/manual use.
    """
    if not MANIFEST_PATH.exists():
        return {"doi": doi}

    with open(MANIFEST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("doi", "")).strip().lower() == doi.lower():
                return record

    return {"doi": doi}


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

    Only returns text for DOIs that have an actual PDF in data/raw/.
    Without this check, every manifest record (thousands of them) would
    get a .txt file written — one fixture-text copy per DOI — even for
    papers that were never downloaded.
    """
    # Require an existing PDF even in dry-run mode so prepare_texts.py does
    # not create fake cache files for papers we haven't downloaded yet.
    record = _manifest_record_for_doi(doi)
    pdf_path = preferred_pdf_path(RAW_DIR, record)
    if not pdf_path.exists():
        return None

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

    raw_text = clean_publisher_noise(raw_text)
    text_no_refs = remove_references_section(raw_text, doi) if REMOVE_REFERENCES else raw_text
    # No truncation here — the cache should hold the full cleaned text.
    # Summarizer applies MAX_INPUT_CHARS when building the LLM prompt.
    return text_no_refs


def _extract_live(doi: str) -> str | None:
    """
    Live path: find PDF in data/raw/, extract, clean.

    The PDF path is resolved from manifest metadata when available so both new
    descriptive filenames and legacy DOI-only filenames work.

    No truncation is applied here. The full cleaned text is returned so that
    prepare_texts.py can cache it completely. The summariser applies its own
    MAX_INPUT_CHARS limit when building the LLM prompt.
    """
    record = _manifest_record_for_doi(doi)
    pdf_path = preferred_pdf_path(RAW_DIR, record)

    if not pdf_path.exists():
        log_error(doi, "extract", f"PDF not found: {pdf_path}")
        return None

    raw_text = extract_text_from_pdf(pdf_path)
    if raw_text is None:
        return None  # Error already logged inside extract_text_from_pdf.

    raw_text = clean_publisher_noise(raw_text)
    text_no_refs = remove_references_section(raw_text, doi) if REMOVE_REFERENCES else raw_text
    return text_no_refs


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
