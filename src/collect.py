"""
src/collect.py — The Ingestion Engine
======================================

WHY DOES THIS MODULE EXIST?
-----------------------------
Before we can summarise veterinary papers, we need a reliable list of *which*
papers exist.  This module builds that list — the "manifest" — by querying the
CrossRef API for up to COLLECT_CANDIDATES_PER_JOURNAL DOIs per journal and
writing them to a JSONL file that download.py consumes.

STRATIFIED CORPUS DESIGN
--------------------------
This pipeline targets exactly 50 PDFs per journal across 5 journals (250
total).  Equal-quota stratified sampling is used instead of proportional
sampling for two reasons:

  1. BIAS PREVENTION
     High-OA journals (e.g. JVIM) publish far more freely available content
     than surgical or radiology journals.  Proportional sampling would let
     JVIM dominate the corpus, potentially biasing the LLM's learned writing-
     style patterns toward internal-medicine content.  Equal quotas prevent this.

  2. SUBGROUP ANALYSIS POWER
     With exactly 50 papers per journal we can run balanced subgroup analyses
     in Phase 4 (e.g. "does summarisation quality differ between JFMS and
     Veterinary Surgery?").  A Kruskal-Wallis test with 5 groups of n=50 has
     adequate power at α=0.05 for medium effect sizes.  A proportional approach
     would leave low-OA journals with far fewer samples (sometimes < 20),
     making subgroup comparisons underpowered.

CANDIDATE BUFFER
-----------------
We collect COLLECT_CANDIDATES_PER_JOURNAL (80) DOIs per journal even though
the download quota is 50.  Not every CrossRef DOI will have a freely
downloadable PDF — publisher embargo, broken links, and PMC deposit lag all
reduce the OA hit rate.  The extra 30 candidates give download.py a fallback
pool so transient OA gaps do not cause the corpus to fall short of 50 PDFs
per journal.  80 candidates at a ~40% paywall rate still yields 48 OA PDFs.

ARCHITECTURAL DECISIONS
------------------------
- WHY CROSSREF (not PubMed, not Scopus)?
    CrossRef is the canonical DOI registry.  The API is free, requires no
    account, and returns structured JSON.  PubMed only covers NIH-indexed
    biomedical articles; it would miss many veterinary surgery papers.  Scopus
    requires a paid institutional key we do not have.

- WHY JSONL (not CSV, not a database)?
    JSON Lines is append-safe.  If the pipeline crashes after paper #47,
    lines 1-47 are already valid.  A CSV would require rewriting the whole
    file; a database adds infrastructure with no benefit at ~250 rows.

- WHY has-license:true AS A CROSSREF PRE-FILTER?
    CrossRef's `has-license:true` filter restricts results to articles that
    have a machine-readable license URL deposited by the publisher.  This is
    NOT a guarantee that a PDF is freely downloadable — some licensed papers
    are CC-BY but the PDF still sits behind an institutional login.  However,
    the filter improves the OA hit rate in the candidate pool and reduces
    wasted API pages.  The real legal gate is the download step (Unpaywall
    `is_oa` check, Semantic Scholar `isOpenAccess` flag, PMC availability).

- WHY sort=published desc?
    Newer papers are more likely to comply with modern OA mandates (e.g. NIH
    2023 policy, cOAlition S plan).  Sorting newest-first biases the candidate
    pool toward higher OA availability, increasing the expected download yield.

- WHY DRY_RUN MODE?
    Lets the researcher test the entire pipeline end-to-end without network
    access.  Critical for laptop development before a scheduled compute run.

TARGET JOURNALS AND ISSNs
--------------------------
  - JVIM  (Journal of Veterinary Internal Medicine) : 1939-1676 (e-ISSN)
  - JAVMA (J. American Veterinary Medical Association) : 0003-1488
  - Veterinary Surgery                               : 0161-3499
  - VRU   (Veterinary Radiology & Ultrasound)        : 1058-8183
  - JFMS  (Journal of Feline Medicine and Surgery)   : 1098-612X

MANIFEST SCHEMA (one JSON object per line)
------------------------------------------
{
  "doi":                "10.1111/jvim.12345",
  "title":              "Evaluation of ...",
  "year":               2024,
  "pub_year":           2024,
  "authors":            ["Smith J", "Jones A"],
  "abstract":           "Objective: ...",
  "journal":            "JVIM",
  "issn":               "1939-1676",
  "species":            ["Canine"],
  "study_design":       "Prospective Observational",
  "clinical_topic":     "Gastroenterology",
  "needs_manual_review": false
}

Note: "year" and "pub_year" carry the same value.  "year" is kept for
backward compatibility with extract.py and existing tests; "pub_year" is
the canonical field name used by the balanced-corpus workflow.
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
# Configuration — Stratified Corpus Design
# ---------------------------------------------------------------------------

# WHY FIVE EQUAL QUOTAS?
# See the module-level docstring for the full rationale.  In short: equal
# quotas prevent bias toward high-OA journals and ensure balanced statistical
# power for subgroup analysis in Phase 4.
JOURNAL_TARGETS: dict[str, int] = {
    "JVIM":               50,
    "JAVMA":              50,
    "Veterinary Surgery": 50,
    "VRU":                50,
    "JFMS":               50,
}

# Canonical ISSNs used for CrossRef queries.
# Electronic ISSNs (e-ISSN) are preferred because CrossRef returns more
# complete metadata (especially license fields and abstracts) for e-ISSNs.
# Where only one ISSN is registered, the single ISSN is used.
TARGET_JOURNALS: dict[str, str] = {
    "JVIM":               "1939-1676",   # e-ISSN (print ISSN: 0891-6640)
    "JAVMA":              "0003-1488",
    "Veterinary Surgery": "0161-3499",
    "VRU":                "1058-8183",
    "JFMS":               "1098-612X",   # corrected from former typo 1098-632X
}

# WHY 80 CANDIDATES FOR A 50-PDF QUOTA?
# Not every CrossRef DOI will resolve to a freely downloadable PDF.
# Collecting 80 candidates gives download.py a 30-paper buffer.
# Even at a 40% paywall rate, 48 OA PDFs are reachable — close enough for
# manual supplementation via src/supplement.py.
COLLECT_CANDIDATES_PER_JOURNAL: int = 80

# Publication window for the study.
DATE_FROM = "2023-01-01"
DATE_TO   = "2025-12-31"

# CrossRef API base URL.  The /works endpoint returns individual articles.
CROSSREF_BASE = "https://api.crossref.org/works"

# 100 results per page: fast API responses while allowing incremental tqdm.
PAGE_SIZE = 100

# Output path for the manifest.  Stored in data/ which is gitignored.
MANIFEST_PATH = Path("data") / "manifest.jsonl"


# ---------------------------------------------------------------------------
# Covariate keyword dictionaries
# ---------------------------------------------------------------------------
# Why keyword matching before an LLM?  Fast, free, and deterministic.
# Papers that keyword inference cannot classify confidently are flagged
# needs_manual_review=true for later correction in Phase 3.

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
        The abstract (or title + abstract) of the paper.
    """
    lower = text.lower()

    # --- Species ---
    found_species = [
        label
        for label, keywords in SPECIES_KEYWORDS.items()
        if any(kw in lower for kw in keywords)
    ]

    # --- Study design (first match wins; designs are treated as mutually exclusive) ---
    found_design = "Unknown"
    for label, keywords in STUDY_DESIGN_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found_design = label
            break

    # --- Clinical topic (first match wins) ---
    found_topic = "Unknown"
    for label, keywords in CLINICAL_TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found_topic = label
            break

    # If we couldn't determine all three fields, flag for manual review.
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
        Phase 2 has no API keys.  This mock returns a transparent placeholder
        that the researcher can see and manually correct in Phase 3.

    WHY NOT LEAVE FIELDS BLANK?
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

    Parameters
    ----------
    issn   : str — Journal ISSN, e.g. "1939-1676".
    offset : int — Number of records to skip (for pagination).

    FILTER RATIONALE
    -----------------
    - type:journal-article
        Excludes editorials, errata, and book chapters that CrossRef sometimes
        returns when querying by ISSN alone.

    - has-license:true
        Pre-filter for papers with a machine-readable license URL deposited by
        the publisher.  NOT a legal guarantee — some CC-BY papers lack a
        deposited license entry, and some "licensed" papers have no free PDF.
        The real OA gate is in download.py (Unpaywall is_oa, Semantic Scholar
        isOpenAccess, PMC availability).  This filter just improves the ratio
        of OA-eligible papers in the candidate pool.

    - sort=published / order=desc
        Newest papers first.  Recent papers are more likely to comply with
        modern OA mandates (NIH 2023 policy, cOAlition S plan), so newest-first
        biases the candidate pool toward higher download yield.

    WHY OFFSET PAGINATION (not cursor)?
        CrossRef supports both.  Offset is simpler to reason about and safe for
        our scale (~80 candidates per journal = one or two pages).
    """
    params = {
        "filter": (
            f"issn:{issn},"
            f"from-pub-date:{DATE_FROM},"
            f"until-pub-date:{DATE_TO},"
            "type:journal-article,"
            # has-license:true is an OA pre-filter, not a legal guarantee.
            # It improves the OA hit rate in the candidate pool, but the
            # download step (Unpaywall / Semantic Scholar / PMC) remains the
            # actual legal compliance gate.
            "has-license:true"
        ),
        "sort":   "published",
        "order":  "desc",
        # Adding "license" to select so _parse_crossref_item can log it if needed.
        "select": "DOI,title,author,abstract,published,container-title,license",
        "rows":   PAGE_SIZE,
        "offset": offset,
        # CrossRef polite-pool identification — not a secret, just contact info.
        "mailto": os.getenv("UNPAYWALL_EMAIL", "researcher@example.com"),
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

    WHY INCLUDE BOTH 'year' AND 'pub_year'?
        'year' was the original field name used in Phase 2.  'pub_year' is the
        canonical field name required by the balanced-corpus workflow (so that
        download.py and supplement.py can query the publication year without
        guessing the field name).  Both carry the same value to maintain
        backward compatibility with extract.py and existing tests.

    WHY GUARD FOR MISSING DOI?
        CrossRef items occasionally lack certain fields.  A missing DOI makes
        the record useless — we can't download or deduplicate without it.
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

    combined_text = f"{title} {abstract}"
    covariates = _infer_covariates(combined_text)

    if covariates["needs_manual_review"]:
        covariates = _mock_covariate_llm(doi)

    return {
        "doi":            doi,
        "title":          title,
        "year":           year,      # kept for backward compatibility with extract.py
        "pub_year":       year,      # canonical field for balanced-corpus workflow
        "authors":        authors,
        "abstract":       abstract,
        "journal":        journal_name,
        "issn":           issn,
        **covariates,
    }


# ---------------------------------------------------------------------------
# Dry-run mock data
# ---------------------------------------------------------------------------

# Two mock papers per journal = 10 total.
#
# WHY TWO PER JOURNAL (not one, not eighty)?
#   - One per journal would not demonstrate the per-journal queue that
#     download.py needs to simulate the balanced scheduling logic.
#   - Eighty per journal (the live candidate count) would slow tests for no
#     extra coverage value.
#   - Two per journal is the minimum that shows "queue has multiple candidates"
#     while keeping the dry-run instant.
#
# All mock papers include pub_year alongside year for schema consistency.
MOCK_PAPERS: list[dict] = [
    # --- JVIM ---
    {
        "doi":            "10.1111/jvim.00001",
        "title":          "Serum cPLI in Dogs with Acute Pancreatitis: A Prospective Study",
        "year":           2024,
        "pub_year":       2024,
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
        "doi":            "10.1111/jvim.00006",
        "title":          "Canine Lymphoma Chemotherapy Response Rates",
        "year":           2024,
        "pub_year":       2024,
        "authors":        ["Davis R", "Kim Y"],
        "abstract":       "Retrospective study of canine lymphoma cases treated with CHOP protocol.",
        "journal":        "JVIM",
        "issn":           "1939-1676",
        "species":        ["Canine"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Oncology",
        "needs_manual_review": False,
    },
    # --- JAVMA ---
    {
        "doi":            "10.2460/javma.00002",
        "title":          "Feline Hypertrophic Cardiomyopathy: Retrospective Review",
        "year":           2023,
        "pub_year":       2023,
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
        "doi":            "10.2460/javma.00007",
        "title":          "Antimicrobial Resistance Patterns in Canine Urinary Tract Infections",
        "year":           2025,
        "pub_year":       2025,
        "authors":        ["Martinez S"],
        "abstract":       "Prospective observational study of antibiotic resistance in dogs with bacterial UTIs.",
        "journal":        "JAVMA",
        "issn":           "0003-1488",
        "species":        ["Canine"],
        "study_design":   "Prospective Observational",
        "clinical_topic": "Infectious Disease",
        "needs_manual_review": False,
    },
    # --- Veterinary Surgery ---
    {
        "doi":            "10.1111/vsu.00003",
        "title":          "Tibial Plateau Leveling Osteotomy Outcomes in Dogs",
        "year":           2024,
        "pub_year":       2024,
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
        "doi":            "10.1111/vsu.00009",
        "title":          "Laparoscopic Splenectomy in Dogs: Complication Rates",
        "year":           2023,
        "pub_year":       2023,
        "authors":        ["Chen W", "Park S"],
        "abstract":       "Retrospective case series of dogs undergoing laparoscopic splenectomy.",
        "journal":        "Veterinary Surgery",
        "issn":           "0161-3499",
        "species":        ["Canine"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Oncology",
        "needs_manual_review": False,
    },
    # --- VRU ---
    {
        "doi":            "10.1111/vru.00004",
        "title":          "Ultrasound Characterization of Feline Hepatic Masses",
        "year":           2025,
        "pub_year":       2025,
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
        "doi":            "10.1111/vru.00010",
        "title":          "CT Features of Canine Adrenal Masses",
        "year":           2024,
        "pub_year":       2024,
        "authors":        ["Robinson A", "Harris L"],
        "abstract":       "Retrospective study of CT imaging findings in dogs with adrenal gland tumors.",
        "journal":        "VRU",
        "issn":           "1058-8183",
        "species":        ["Canine"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Endocrinology",
        "needs_manual_review": False,
    },
    # --- JFMS ---
    {
        "doi":            "10.1177/jfms.00005",
        "title":          "Feline Diabetes Mellitus: A Systematic Review of Treatment Protocols",
        "year":           2023,
        "pub_year":       2023,
        "authors":        ["Thompson L", "Nguyen P"],
        "abstract":       "Systematic review and meta-analysis of insulin therapy in diabetic cats.",
        "journal":        "JFMS",
        "issn":           "1098-612X",
        "species":        ["Feline"],
        "study_design":   "Systematic Review",
        "clinical_topic": "Endocrinology",
        "needs_manual_review": False,
    },
    {
        "doi":            "10.1177/jfms.00011",
        "title":          "Feline Chronic Kidney Disease: Staging and Outcome",
        "year":           2024,
        "pub_year":       2024,
        "authors":        ["O'Brien M", "Scott T"],
        "abstract":       "Retrospective medical records review of cats with chronic kidney disease.",
        "journal":        "JFMS",
        "issn":           "1098-612X",
        "species":        ["Feline"],
        "study_design":   "Retrospective Case Series",
        "clinical_topic": "Endocrinology",
        "needs_manual_review": False,
    },
]


# ---------------------------------------------------------------------------
# Core collection function
# ---------------------------------------------------------------------------

def run_collection() -> int:
    """
    Build or extend the paper manifest using stratified per-journal quotas.

    STRATIFIED SAMPLING LOGIC
    --------------------------
    For each of the five target journals, we query CrossRef until we have
    COLLECT_CANDIDATES_PER_JOURNAL (80) candidate DOIs or the API returns no
    more results.  A DOI deduplication set prevents the same paper from
    appearing twice in a single run (rare with CrossRef offset pagination, but
    possible if the same DOI is returned on multiple pages).

    The per-journal quota enforces the balanced corpus design: even if CrossRef
    returns 500 papers for JVIM but only 60 for Veterinary Surgery, both
    journals contribute the same number of candidates to the download pool.

    IDEMPOTENCY NOTE
    -----------------
    This function appends to data/manifest.jsonl rather than overwriting it.
    If you re-run collection, duplicate DOIs from different runs may appear.
    For a clean fresh run, delete data/manifest.jsonl before calling this
    function (the README has instructions).

    In DRY_RUN mode: writes MOCK_PAPERS (2 per journal, 10 total) without any
    network calls.

    In live mode: pages through CrossRef for each journal with has-license:true
    and type:journal-article filters, stopping at COLLECT_CANDIDATES_PER_JOURNAL.

    Returns
    -------
    int
        Number of papers successfully written to the manifest.
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    papers_written = 0

    if dry_run:
        print("[collect] DRY_RUN=true — writing mock manifest entries (no network calls).")
        per_journal_counts: dict[str, int] = {}
        with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
            for paper in tqdm(MOCK_PAPERS, desc="Writing mock papers"):
                f.write(json.dumps(paper) + "\n")
                papers_written += 1
                j = paper["journal"]
                per_journal_counts[j] = per_journal_counts.get(j, 0) + 1

        print("[collect] DRY_RUN per-journal candidate counts:")
        for journal, count in per_journal_counts.items():
            target = JOURNAL_TARGETS.get(journal, 50)
            print(f"  {journal:<25} {count} mock candidates  (download target: {target})")
        print(f"[collect] Wrote {papers_written} mock papers to {MANIFEST_PATH}")
        return papers_written

    # --- Live mode ---
    print(
        f"[collect] Live mode -- querying CrossRef for {len(TARGET_JOURNALS)} journals "
        f"({DATE_FROM} to {DATE_TO}).\n"
        f"[collect] Collecting up to {COLLECT_CANDIDATES_PER_JOURNAL} candidates per journal "
        f"(download quota: {max(JOURNAL_TARGETS.values())} PDFs per journal)."
    )

    # In-run DOI deduplication.
    # WHY A SET (not checking the existing manifest file)?
    #   Reading the whole manifest file on every DOI write would be O(n^2).
    #   A set is O(1) per lookup.  It only deduplicates within a single run;
    #   cross-run deduplication is handled by deleting the manifest before re-run.
    seen_dois: set[str] = set()

    for journal_name, issn in TARGET_JOURNALS.items():
        print(f"\n[collect] Fetching '{journal_name}' (ISSN {issn})...")
        journal_count = 0
        offset = 0

        with open(MANIFEST_PATH, "a", encoding="utf-8") as manifest_file:
            while journal_count < COLLECT_CANDIDATES_PER_JOURNAL:
                items = _fetch_crossref_page(issn, offset)

                if not items:
                    # API returned an empty page — no more results for this journal.
                    break

                for item in tqdm(
                    items,
                    desc=f"  {journal_name} offset={offset}",
                    leave=False,
                ):
                    if journal_count >= COLLECT_CANDIDATES_PER_JOURNAL:
                        # Reached this journal's candidate quota; stop consuming
                        # the current page (avoids a partial page fetch waste).
                        break

                    parsed = _parse_crossref_item(item, journal_name, issn)
                    if parsed is None:
                        log_error("N/A", "collect", f"Skipped item without DOI in {journal_name}")
                        continue

                    doi = parsed["doi"]
                    if doi in seen_dois:
                        # Duplicate DOI within this run — skip silently.
                        continue

                    seen_dois.add(doi)
                    manifest_file.write(json.dumps(parsed) + "\n")
                    journal_count += 1
                    papers_written += 1

                offset += PAGE_SIZE

                # CrossRef polite-pool rate limit: ≤ 1 request per second.
                time.sleep(1.0)

        target = JOURNAL_TARGETS.get(journal_name, 50)
        if journal_count >= COLLECT_CANDIDATES_PER_JOURNAL:
            status = f"{journal_count} candidates"
        else:
            # API ran out of results before we hit our quota — this is normal
            # for smaller or lower-OA journals.  download.py will handle the
            # shortfall by logging it and flagging for manual supplementation.
            status = (
                f"{journal_count} candidates "
                f"(API exhausted before reaching {COLLECT_CANDIDATES_PER_JOURNAL})"
            )
        print(f"[collect] {journal_name}: {status}  (download target: {target})")

    print(f"\n[collect] Done. Total candidates written to manifest: {papers_written}")
    return papers_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    count = run_collection()
    print(f"Collection complete. Manifest contains {count} new entries.")
