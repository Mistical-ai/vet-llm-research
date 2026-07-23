"""One-off, targeted re-judge of the 2 Gemini RECITATION-blocked rows using
a paraphrase-only prompt variant (llm-sum/prompts/judge_medhelm_v1_paraphrase.txt).

This is a LIVE API call — run it yourself, not via Claude, per project rules.
It does NOT touch the default judge prompt file or any pipeline default, so
every other/future evaluate run is completely unaffected.

Run from the repo root:
    python retry_recitation_blocked.py
(or wherever you've saved this file — it inserts llm-sum/ on sys.path itself)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "llm-sum")

from evaluator import call_judge, build_evaluation_row, append_evaluation, _is_dry_run
from eval_instances import build_strata, load_manifest_index
from prepare_texts import read_cached_text
from file_paths import doi_to_slug

# This script bypasses the normal evaluate CLI (and its confirm_real_judge()
# safety prompt) entirely, so it re-implements the same guardrail here:
# never spend real money without an explicit human "yes".
if _is_dry_run():
    print("DRY_RUN is active (or PHASE3_MODE=test) — this would call the mock "
          "judge, not the real Gemini API. Set DRY_RUN=false in your shell/.env "
          "and re-run if you actually want a live call.")
    sys.exit(1)

print("This will make 2 REAL, paid Gemini API calls (one per row below).")
if input("Type 'yes' to confirm: ").strip().lower() != "yes":
    print("Not confirmed — exiting without calling anything.")
    sys.exit(0)

TARGETS = [
    ("10.2460/javma.22.12.0585", "openai"),
    ("10.1111/vsu.14259", "gemini"),
]

PARAPHRASE_PROMPT_PATH = Path("llm-sum/prompts/judge_medhelm_v1_paraphrase.txt")
SUMMARIES_PATH = Path("data/summaries.jsonl")

paraphrase_template = PARAPHRASE_PROMPT_PATH.read_text(encoding="utf-8")
for placeholder in ("{REFERENCE_TEXT}", "{CANDIDATE_SUMMARY}"):
    assert placeholder in paraphrase_template, f"missing {placeholder} in paraphrase prompt"

summaries_by_doi: dict[str, dict] = {}
with open(SUMMARIES_PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            entry = json.loads(line)
            doi = str(entry.get("doi", "")).strip()
            if doi:
                summaries_by_doi[doi] = entry

manifest_index = load_manifest_index()

for doi, summariser in TARGETS:
    entry = summaries_by_doi.get(doi)
    if entry is None:
        print(f"SKIP {doi}: not found in summaries.jsonl")
        continue
    input_source = str(entry.get("input_source") or "processed")
    candidate_summary = (entry.get("models") or {}).get(summariser, {}).get("summary", "")
    if not candidate_summary:
        print(f"SKIP {doi}/{summariser}: no candidate summary text found")
        continue

    reference_text = read_cached_text(entry, input_source=input_source)
    if reference_text is None:
        print(f"SKIP {doi}/{summariser}: no cached reference text found")
        continue

    print(f"Calling gemini judge for {doi} / {summariser} (paraphrase-only prompt)...")
    response = call_judge(
        "gemini", reference_text, candidate_summary,
        prompt_template=paraphrase_template,
        article_id=doi_to_slug(doi),
    )

    manifest_record = manifest_index.get(doi, {})
    strata = build_strata(entry, manifest_record)

    row = build_evaluation_row(
        doi=doi, summariser=summariser, judge="gemini",
        input_source=input_source, reference_text=reference_text,
        candidate_summary=candidate_summary, judge_response=response,
        strata=strata,
    )
    # Audit marker — NOT a standard schema field, purely so this deviation
    # from the standard judge_medhelm_v1.txt prompt stays traceable in the
    # ledger. Does not affect parsing, scoring, or any other row.
    row["judge_prompt_variant"] = "paraphrase_quotes_v1"

    if row.get("parse_method") == "sentinel":
        print(f"  STILL BLOCKED: {doi}/{summariser} — appending anyway for the record.")
    else:
        print(f"  RECOVERED: {doi}/{summariser} — jury_score={row.get('jury_score')}")

    append_evaluation(row)

print("\nDone. Re-run eval-report to see the updated numbers.")
