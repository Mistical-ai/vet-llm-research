"""
src/collect.py — The Ingestion Engine
======================================

WHY DOES THIS MODULE EXIST?
-----------------------------
Before we can summarise veterinary papers, we need a reliable list of *which*
papers exist.  This module builds that list — the "manifest" — by querying the
CrossRef API, which is a free, open index of scholarly publications.

ARCHITECTURAL DECISIONS
------------------------
- WHY CROSSREF (not PubMed, not Scopus)?
    CrossRef is the canonical DOI registry.  Every journal article has a DOI
    registered there.  The API is free, requires no account, and returns
    structured JSON with the exact fields we need (title, authors, abstract,
    year).  PubMed only covers biomedical articles indexed by NIH; it would
    miss some veterinary surgery papers.  Scopus requires a paid institutional
    key we don't have.

- WHY JSONL (not CSV, not a database)?
    JSON Lines (one JSON object per line) is append-safe.  If the pipeline
    crashes after processing paper #47, lines 1-47 are already written and
    valid.  A CSV would require re-writing the whole file.  A database adds
    infrastructure complexity with no benefit at this scale (~250 rows).

- WHY KEYWORD-BASED COVARIATE INFERENCE (not an LLM in Phase 2)?
    We have no API keys yet.  Even when we do, running an LLM call per paper
    for metadata that can often be inferred from the abstract with a simple
    keyword scan would waste budget.  The keyword approach covers ~70% of
    papers for free; the remaining 30% are flagged needs_manual_review: true
    and will be handled in Phase 3.

- WHY DRY_RUN MODE?
    It lets the researcher test the entire pipeline end-to-end — including
    file I/O, covariate inference, and the manifest schema — without network
    access or API rate limits.  This is critical for developing on a laptop
    before a scheduled compute run.

TARGET JOURNALS AND ISSNs
--------------------------
  - JVIM  (Journal of Veterinary Internal Medicine) : 1939-1676
  - JAVMA (Journal of the American Veterinary Medical Association) : 0003-1488
  - Veterinary Surgery                               : 0161-3499
  - VRU   (Veterinary Radiology & Ultrasound)        : 1058-8183
  - JFMS  (Journal of Feline Medicine and Surgery)   : 1098-632X

MANIFEST SCHEMA (one JSON object per line)
------------------------------------------
{
  "doi":            "10.1111/jvim.12345",
  "title":          "Evaluation of ...",
  "year":           2024,
  "authors":        ["Smith J", "Jones A"],
  "abstract":       "Objective: ...",
  "journal":        "JVIM",
  "issn":           "1939-1676",
  "species":        ["Canine"],
  "study_design":   "Prospective Observational",
  "clinical_topic": "Gastroenterology",
  "needs_manual_review": false
}
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

from utils import log_error

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The five target journals.  Key = short display name, value = ISSN.
# Why store ISSNs? CrossRef's filter API accepts `issn:XXXX-XXXX` as a query
# parameter, making it the most reliable way to restrict results to a journal.
TARGET_JOURNALS: dict[str, str] = {
    "JVIM":               "1939-1676",
    "JAVMA":              "0003-1488",
    "Veterinary Surgery": "0161-3499",
    "VRU":                "1058-8183",
    "JFMS":               "1098-632X",
}

# Publication window for the study.
DATE_FROM = "2023-01-01"
DATE_TO   = "2025-12-31"

# CrossRef API base URL.  The /works endpoint returns individual articles.
CROSSREF_BASE = "https://api.crossref.org/works"

# How many results to request per API page.  CrossRef's max is 1000, but 100
# keeps each response fast and lets tqdm show progress incrementally.
PAGE_SIZE = 100

# Output path for the manifest.  Why data/?  All pipeline outputs live there
# and are gitignored so we never accidentally commit 250 paper records.
MANIFEST_PATH = Path("data") / "manifest.jsonl"

# ---------------------------------------------------------------------------
# Covariate keyword dictionaries
# ---------------------------------------------------------------------------
# Why keyword matching before LLM?  Fast, free, deterministic, and reproducible.
# An LLM call with temperature=0 and seed=42 is also deterministic, but it
# costs money and requires an API key we don't have yet.

SPECIES_KEYWORDS: dict[str, list[str]] = {
    "Canine":  ["dog", "dogs", "canine", "canines", "puppy", "puppies"],
    "Feline":  ["cat", "cats", "feline", "felines", "kitten", "kittens"],
    "Equine":  ["horse", "horses", "equine", "equines", "foal", "foals", "mare"],
    "Bovine":  ["cow", "cows", "bovine", "cattle", "calf", "calves", "bull"],
    "Avian":   ["bird", "birds", "avian", "parrot", "parrots", "raptor"],
}

STUDY_DESIGN_KEYWORDS: dict[str, list[str]] = {
    "RCT":                        ["randomized controlled", "randomised controlled", "rct"],
    "Prospective Observational":  ["prospective", "cohort study", "observational study"],
    "Retrospective Case Series":  ["retrospective", "medical records", "case series"],
    "Systematic Review":          ["systematic review", "meta-analysis", "meta analysis"],
    "Case Report":                ["case report", "case presentation"],
}

CLINICAL_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "Oncology":          ["tumor", "tumour", "cancer", "neoplasia", "lymphoma", "sarcoma"],
    "Cardiology":        ["cardiac", "heart", "arrhythmia", "cardiomyopathy", "echocardiograph"],
    "Gastroenterology":  ["gastro", "pancreat", "intestin", "hepat", "liver", "vomit", "diarrhea"],
    "Orthopedics":       ["fracture", "orthoped", "bone", "joint", "cruciate", "arthr"],
    "Neurology":         ["neuro", "seizure", "spinal", "intervertebral", "disc", "epileps"],
    "Dermatology":       ["skin", "dermat", "atop", "pruritus", "alopecia"],
    "Infectious Disease": ["infection", "bacterial", "viral", "parasit", "antimicrobial", "antibiotic"],
    "Endocrinology":     ["diabet", "thyroid", "adrenal", "cushings", "hyperadrenocorticism"],
}


def _infer_covariates(text: str) -> dict:
    """
    Attempt to infer species, study design, and clinical topic from abstract text.

    Returns a dict with keys: species, study_design, clinical_topic,
    needs_manual_review.

    WHY RETURN needs_manual_review INSTEAD OF GUESSING?
        If we can't confidently classify a paper, silently assigning a wrong
        label would corrupt the stratified analysis in Phase 4.  It's better
        to flag uncertainty explicitly so the researcher can review those rows.

    Parameters
    ----------
    text : str
        The abstract (or title + abstract) of the paper, lowercased by caller.
    """
    lower = text.lower()

    # --- Species ---
    found_species = [
        label
        for label, keywords in SPECIES_KEYWORDS.items()
        if any(kw in lower for kw in keywords)
    ]

    # --- Study design (take the first match; designs are mutually exclusive) ---
    found_design = "Unknown"
    for label, keywords in STUDY_DESIGN_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found_design = label
            break

    # --- Clinical topic (take the first match) ---
    found_topic = "Unknown"
    for label, keywords in CLINICAL_TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found_topic = label
            break

    # If we couldn't determine all three fields, flag for manual review.
    # This preserves data quality without needing a live LLM call.
    needs_review = (
        len(found_species) == 0
        or found_design == "Unknown"
        or found_topic == "Unknown"
    )

    return {
        "species":             found_species if found_species else [],
        "study_design":        found_design,
        "clinical_topic":      found_topic,
        "needs_manual_review": needs_review,
    }


def _mock_covariate_llm(doi: str) -> dict:
    """
    Fake LLM call used when keyword inference fails completely.

    WHY MOCK INSTEAD OF REAL LLM?
        Phase 2 has no API keys.  Even in future phases, calling an LLM for
        every paper that stumps the keyword matcher would be expensive.  This
        mock returns a transparent placeholder that the researcher can see
        and manually correct.

    WHY NOT JUST LEAVE THE FIELDS BLANK?
        A blank field is ambiguous — did inference fail, or was the field never
        attempted?  needs_manual_review: true is an explicit signal.

    Parameters
    ----------
    doi : str — The DOI of the paper being processed.
    """
    print(f"[mock_llm] No API key — returning placeholder covariates for {doi}")
    return {
        "species":             [],
        "study_design":        "Unknown",
        "clinical_topic":      "Unknown",
        "needs_manual_review": True,
    }


# ---------------------------------------------------------------------------
# CrossRef API helpers
# ---------------------------------------------------------------------------

def _fetch_crossref_page(issn: str, offset: int) -> list[dict]:
    """
    Fetch one page of CrossRef results for a single ISSN.

    WHY PAGINATE?
        CrossRef caps a single response at 1000 rows.  For journals with
        thousands of articles per year, we must page through results.  Using
        offset-based pagination (rather than cursor-based) is simpler and
        sufficient for ~250 target papers.

    Parameters
    ----------
    issn   : str — Journal ISSN, e.g. "1939-1676".
    offset : int — Number of records to skip (for pagination).
    """
    params = {
        "filter":   f"issn:{issn},from-pub-date:{DATE_FROM},until-pub-date:{DATE_TO}",
        "select":   "DOI,title,author,abstract,published,container-title",
        "rows":     PAGE_SIZE,
        "offset":   offset,
        # CrossRef's polite pool is faster and more reliable if we identify ourselves.
        # This is not an API key — it's just our contact email.
        "mailto":   os.getenv("UNPAYWALL_EMAIL", "researcher@example.com"),
    }

    try:
        response = requests.get(CROSSREF_BASE, params=params, timeout=30)
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
        return items
    except requests.RequestException as exc:
        print(f"[collect] CrossRef request failed for ISSN {issn}: {exc}")
        return []


def _parse_crossref_item(item: dict, journal_name: str, issn: str) -> dict | None:
    """
    Convert a raw CrossRef API item into our manifest schema.

    Returns None if the item lacks a DOI (the only mandatory field).

    WHY GUARD FOR MISSING DOI?
        CrossRef items occasionally lack certain fields (especially abstract).
        A missing DOI, however, makes the record useless — we can't download
        or deduplicate without it.
    """
    doi = item.get("DOI", "").strip()
    if not doi:
        return None

    # Title is a list in CrossRef; take the first element or fall back.
    raw_title = item.get("title", [])
    title = raw_title[0] if raw_title else "No title available"

    # Year comes from published → date-parts → [[year, month, day]].
    year_parts = item.get("published", {}).get("date-parts", [[None]])
    year = year_parts[0][0] if year_parts[0] else None

    # Authors: CrossRef provides [{given, family, ...}]; we join to "Family G".
    raw_authors = item.get("author", [])
    authors = [
        f"{a.get('family', '')} {a.get('given', '')[:1]}".strip()
        for a in raw_authors
    ]

    abstract = item.get("abstract", "")

    # Infer covariates from title + abstract text.
    combined_text = f"{title} {abstract}"
    covariates = _infer_covariates(combined_text)

    # If all three covariate fields are Unknown/empty, call the mock LLM.
    if covariates["needs_manual_review"]:
        covariates = _mock_covariate_llm(doi)

    return {
        "doi":            doi,
        "title":          title,
        "year":           year,
        "authors":        authors,
        "abstract":       abstract,
        "journal":        journal_name,
        "issn":           issn,
        **covariates,
    }


# ---------------------------------------------------------------------------
# Dry-run mock data
# ---------------------------------------------------------------------------

# These 8 fake papers cover all five journals and all three covariate
# categories so the dry-run exercises every code path in the pipeline.
MOCK_PAPERS: list[dict] = [
    {
        "doi":            "10.1111/jvim.00001",
        "title":          "Serum cPLI in Dogs with Acute Pancreatitis: A Prospective Study",
        "year":           2024,
        "authors":        ["Smith J", "Patel R"],
        "abstract":       "Prospective observational study of canine acute pancreatitis using cPLI.",
        "journal":        "JVIM",
        "issn":           "1939-1676",
        "species":        ["Canine"],
        "study_design":   "Prospective Observational",
        "clinical_topic": "Gastroenterology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.2460/javma.00002",
        "title":          "Feline Hypertrophic Cardiomyopathy: Retrospective Review",
        "year":           2023,
        "authors":        ["Jones A", "Lee C"],
        "abstract":       "Retrospective medical records review of cats with hypertrophic cardiomyopathy.",
        "journal":        "JAVMA",
        "issn":           "0003-1488",
        "species":        ["Feline"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Cardiology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1111/vsu.00003",
        "title":          "Tibial Plateau Leveling Osteotomy Outcomes in Dogs",
        "year":           2024,
        "authors":        ["Garcia M", "Brown T"],
        "abstract":       "Prospective cohort study of canine cruciate ligament repair using TPLO.",
        "journal":        "Veterinary Surgery",
        "issn":           "0161-3499",
        "species":        ["Canine"],
        "study_design":   "Prospective Observational",
        "clinical_topic": "Orthopedics",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1111/vru.00004",
        "title":          "Ultrasound Characterization of Feline Hepatic Masses",
        "year":           2025,
        "authors":        ["Wilson K"],
        "abstract":       "Retrospective case series of cats with hepatic masses evaluated by ultrasound.",
        "journal":        "VRU",
        "issn":           "1058-8183",
        "species":        ["Feline"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Gastroenterology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1177/jfms.00005",
        "title":          "Feline Diabetes Mellitus: A Systematic Review of Treatment Protocols",
        "year":           2023,
        "authors":        ["Thompson L", "Nguyen P"],
        "abstract":       "Systematic review and meta-analysis of insulin therapy in diabetic cats.",
        "journal":        "JFMS",
        "issn":           "1098-632X",
        "species":        ["Feline"],
        "study_design":   "Systematic Review",
        "clinical_topic": "Endocrinology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1111/jvim.00006",
        "title":          "Canine Lymphoma Chemotherapy Response Rates",
        "year":           2024,
        "authors":        ["Davis R", "Kim Y"],
        "abstract":       "Retrospective study of canine lymphoma cases treated with CHOP protocol.",
        "journal":        "JVIM",
        "issn":           "1939-1676",
        "species":        ["Canine"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Oncology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.2460/javma.00007",
        "title":          "Antimicrobial Resistance Patterns in Canine Urinary Tract Infections",
        "year":           2025,
        "authors":        ["Martinez S"],
        "abstract":       "Prospective observational study of antibiotic resistance in dogs with bacterial UTIs.",
        "journal":        "JAVMA",
        "issn":           "0003-1488",
        "species":        ["Canine"],
        "study_design":   "Prospective Observational",
        "clinical_topic": "Infectious Disease",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1111/jvim.00008",
        "title":          "Equine Neurological Examination Findings in Spinal Cord Disease",
        "year":           2024,
        "authors":        ["Anderson F", "Chen W"],
        "abstract":       "Case series of horses with intervertebral disc disease and spinal cord compression.",
        "journal":        "JVIM",
        "issn":           "1939-1676",
        "species":        ["Equine"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Neurology",
        "needs_manual_review": True,
    },
]


# ---------------------------------------------------------------------------
# Core collection function
# ---------------------------------------------------------------------------

def run_collection() -> int:
    """
    Build or extend the paper manifest.

    In DRY_RUN mode: writes MOCK_PAPERS to the manifest file without any
    network calls.

    In live mode: pages through CrossRef for each target journal and writes
    matching papers incrementally (append-safe JSONL).

    Returns
    -------
    int
        Number of papers successfully written to the manifest.
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    # Ensure the data/ directory exists before writing.
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    papers_written = 0

    if dry_run:
        print("[collect] DRY_RUN=true — writing mock manifest entries (no network calls).")
        with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
            for paper in tqdm(MOCK_PAPERS, desc="Writing mock papers"):
                f.write(json.dumps(paper) + "\n")
                papers_written += 1
        print(f"[collect] Wrote {papers_written} mock papers to {MANIFEST_PATH}")
        return papers_written

    # --- Live mode ---
    print(
        f"[collect] Live mode — querying CrossRef for {len(TARGET_JOURNALS)} journals "
        f"({DATE_FROM} → {DATE_TO})."
    )

    for journal_name, issn in TARGET_JOURNALS.items():
        print(f"\n[collect] Fetching '{journal_name}' (ISSN {issn})...")
        offset = 0

        with open(MANIFEST_PATH, "a", encoding="utf-8") as manifest_file:
            while True:
                items = _fetch_crossref_page(issn, offset)

                if not items:
                    # No more results for this journal.
                    break

                for item in tqdm(items, desc=f"  {journal_name} offset={offset}", leave=False):
                    parsed = _parse_crossref_item(item, journal_name, issn)
                    if parsed is None:
                        log_error("N/A", "collect", f"Skipped item without DOI in {journal_name}")
                        continue

                    manifest_file.write(json.dumps(parsed) + "\n")
                    papers_written += 1

                offset += PAGE_SIZE

                # Polite delay between pages to stay within CrossRef's rate limit.
                # CrossRef asks for at most 1 request/second in the polite pool.
                time.sleep(1.0)

    print(f"\n[collect] Done. Total papers written: {papers_written}")
    return papers_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    count = run_collection()
    print(f"Collection complete. Manifest contains {count} new entries.")
