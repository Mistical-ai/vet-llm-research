# Phase 4 — Scenarios, offline rubric, and run provenance

This guide explains the MedHELM-inspired additions on the `medhelmv1` branch:
named **scenarios**, an optional **offline rubric** for free sanity checks, and
**run manifests** that record what produced each evaluation row.

> **Golden rule:** Phase 3's blind judge (`llm-sum/evaluator.py` →
> `data/evaluations.jsonl`) is the **authoritative** study endpoint. Everything
> in this document is auxiliary unless your methods section says otherwise.

---

## 1. Why these pieces exist

MedHELM-style benchmarks share three habits this project adopted in a minimal form:

| MedHELM idea | What we built | Where it lives |
|--------------|---------------|----------------|
| Named scenario with fixed inputs | `PrimaryResearchCorpusScenario`, `VeterinarySummaryQualityScenario` | `src/scenarios/` |
| Versioned rubric + structured scores | `rubric_v1.yaml` + heuristic scorer | `docs/rubrics/`, `src/evaluation/` |
| Run provenance (code, data, prompts) | `RunManifest` written before judges run | `llm-sum/run_manifest.py` |

**What we deliberately did *not* import:** the HELM Python framework, PyYAML as a
new dependency, or a scenario registry that auto-discovers plugins. The goal is
auditability without turning a research pipeline into a framework migration.

---

## 2. Scenarios (`src/scenarios/`)

A **scenario** is a reproducible view of the pipeline: stable name, input paths,
and selection rules.  The base contract is small on purpose:

```text
Scenario
  name, description
  paths: ScenarioPaths   # manifest, raw PDFs, processed cache, summaries
  records()              # yield input rows for this view
  metadata()             # stable dict for logs/tests
```

### `PrimaryResearchCorpusScenario` (used today)

**CLI:** `python pipeline.py` (default) or `python pipeline.py --scenario primary_research_corpus`

**What it does:**

1. Loads `data/manifest.jsonl` (OA) and `data/manual_manifest.jsonl` (optional).
2. Deduplicates by DOI — OA rows win when both sources list the same DOI.
3. Validates manual rows only when a matching PDF exists in `data/raw/`.
4. Classifies each PDF as **primary** (counts toward quota), **secondary**
   (`2_` prefix — reviews kept but not in quota), or **missing**.
5. Prints the familiar per-journal scoreboard and returns exit code `1` when
   primary PDFs fall below the OA threshold (200).

**Why policy lives here instead of in `pipeline.py`:** the scoreboard rules
(primary vs `2_` secondary, journal targets, OA threshold) are testable data
transformations.  `pipeline.py` only orchestrates and prints.

### `VeterinarySummaryQualityScenario` (Phase 3 bridge)

**Not wired to a CLI yet.**  It wraps `llm-sum/eval_instances.py` and yields
the same `EvaluationInstance` rows Phase 3 uses for stratified judging — species,
study design, clinical topic, journal, input source — without calling any API.

Use this when you want a single named object for “the summarization quality
benchmark” in tests or future orchestration.

### Path configuration

`ScenarioPaths` reads from `.env` so manual edits stay in one place:

| Variable | Default | Used for |
|----------|---------|----------|
| `PROCESSED_DIR_NAME` | `processed` | `data/<name>/` text cache |
| `SUMMARIES_JSONL_PATH` | `data/summaries.jsonl` | summary slots to score |

Manifest and raw PDF paths stay at `data/manifest.jsonl` and `data/raw/` for now.

---

## 3. Offline rubric (`--use-rubric`)

### When to use it

- **Good for:** quick, free checks that summaries exist and reference text
  grounding looks reasonable before you pay for judges.
- **Not a substitute for:** blind LLM jury scores in `data/evaluations.jsonl`.

### Command

```powershell
python pipeline.py --use-rubric
```

This still prints the normal corpus status report, then writes heuristic scores
to `data/rubric_scores.jsonl` (separate file — never appended to evaluations).

### Two rubric tracks (do not confuse them)

| Track | Rubric id | Scorer | Output |
|-------|-----------|--------|--------|
| **Authoritative (Phase 3)** | `vet_medhelm_score_v1.0` | Live blind judges | `data/evaluations.jsonl` |
| **Auxiliary (Phase 4)** | `rubric_v1` | Deterministic heuristics | `data/rubric_scores.jsonl` |

The offline rubric uses four 1–5 dimensions defined in
[`docs/rubrics/rubric_v1.yaml`](../rubrics/rubric_v1.yaml):

- `factual_accuracy` — token overlap between summary and reference text
- `hallucination_risk` — inverse of unsupported-looking claims
- `clinical_relevance` — species + clinical keyword presence
- `completeness` — coverage of objective/methods/results/limitations cues

**Reference text precedence** (matches MedHELM jury intent):

1. `data/<PROCESSED_DIR_NAME>/*.jsonl` full cleaned text (preferred)
2. manifest `abstract`
3. manifest `title`

### Environment variables

| Variable | Default |
|----------|---------|
| `OFFLINE_RUBRIC_PATH` | `docs/rubrics/rubric_v1.yaml` |
| `RUBRIC_SCORES_PATH` | `data/rubric_scores.jsonl` |
| `SUMMARIES_JSONL_PATH` | `data/summaries.jsonl` |
| `PROCESSED_DIR_NAME` | `processed` (use `processedv2` when that is your cache) |

### Example output row

```json
{
  "doi": "10.1111/jvim.16872",
  "summarizer": "openai",
  "rubric_version": "rubric_v1",
  "scores": {
    "factual_accuracy": {"score": 4, "rationale": "..."},
    "hallucination_risk": {"score": 5, "rationale": "..."},
    "clinical_relevance": {"score": 4, "rationale": "..."},
    "completeness": {"score": 3, "rationale": "..."}
  },
  "overall_score": 4.0
}
```

`overall_score` is the unweighted mean of the four dimension scores.

---

## 4. Run manifests (Phase 3 evaluate)

Every `python llm-sum/run_phase3.py evaluate` run writes a provenance JSON file
**before** any judge is called, then patches it in a `finally` block when scoring
finishes (success, partial failure, or exception).

**Location:** `data/<PROCESSED_DIR_NAME>/run_manifest_<run_id>.json`

**Why:** if two runs disagree on `jury_score`, diff manifests instead of memory.
Each file records git SHA, branch, dataset hash, judge model IDs, resolved model
versions from API responses, prompt SHA-256, rubric version, and aggregation mode.

See the **Run Manifest** subsection in [phase3/README.md](../phase3/README.md#run-manifest)
for a field-by-field example.

Implementation: `llm-sum/run_manifest.py` (no import of `evaluator.py` — keeps
unit tests isolated and avoids circular imports).

---

## 5. Jury score aggregation (Phase 3, documented here for comparison)

Phase 3 always stores **both**:

- `jury_score_weighted` — clinical-risk-weighted mean (this project's default formula)
- `jury_score_unweighted` — flat mean across criteria × judges (stock MedHELM style)

`JURY_AGGREGATION_MODE` in `.env` picks which value is copied into the primary
`jury_score` field for convenience.  Switching modes never requires re-running
judges.

Full formulas: [medhelm_evaluation.md](../phase3/medhelm_evaluation.md).

---

## 6. File map

```text
pipeline.py                          CLI: corpus status + optional --use-rubric
src/scenarios/
  base.py                            Scenario ABC + ScenarioPaths
  corpus_status.py                   PrimaryResearchCorpusScenario
  summarization_quality.py           VeterinarySummaryQualityScenario
src/evaluation/
  rubric_scoring.py                  Offline heuristic scorer
docs/rubrics/rubric_v1.yaml          Auxiliary rubric definition
llm-sum/run_manifest.py              Evaluate-run provenance records
data/rubric_scores.jsonl             Offline rubric output (gitignored)
data/evaluations.jsonl               Live judge output (gitignored)
```

---

## 7. Tests

```powershell
python -m pytest tests/test_scenarios.py tests/test_rubric_scoring.py tests/test_run_manifest.py tests/test_run_phase3.py -q
```

These tests mock every paid API call.  Safe to run in CI or before pushing.

---

## 8. Related reading

- [GUIDE.md](../GUIDE.md) — project-wide structure and methods
- [phase3/README.md](../phase3/README.md) — summarization and evaluation walkthrough
- [medhelm_evaluation.md](../phase3/medhelm_evaluation.md) — live judge rubric and jury math
