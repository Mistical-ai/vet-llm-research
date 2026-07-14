# evaluator.py

> **Doc role: operator how-to.** This page covers the CLI, file paths, and
> troubleshooting for running `evaluator.py`. For the authoritative rubric
> definition and jury-score math, see
> [MedHELM-Style Evaluation](medhelm_evaluation.md). For the superseded
> Vet-Score v2.0 rubric, see the [legacy reference](judge_and_rubric.md).

## What it does

Reads every `(paper, summariser)` pair from `data/summaries.jsonl` and asks a **judge model** to score the summary against the original cleaned text. Records quality (1-10), hallucination count, hallucination categories, confidence (1-5), and a `requires_human_review` flag for the Phase-5 sample. The judge is **blind to the summariser identity** — the prompt template only sees `{REFERENCE_TEXT}` and `{CANDIDATE_SUMMARY}`.

Real-time only. Batch evaluation submissions are handled by `batch_utils.py` alongside summarisation, and the results are collected by `check_batch_status.py`.

## When to run it

* After `data/summaries.jsonl` has at least one row with `status=success`.
* In `single`/`dev` mode whenever you change the judge prompt configured by `JUDGE_PROMPT_FILE` (default: `llm-sum/prompts/judge_medhelm_v1.txt`).

## Inputs

| Path                                  | Role                                                                       |
|---------------------------------------|----------------------------------------------------------------------------|
| `data/summaries.jsonl`                | Source of (paper, summariser, summary) triples.                            |
| `data/processed/*.jsonl`              | Cleaned reference text the judge compares against.                         |
| `data/evaluations.jsonl` (if exists)  | Used by `--resume` (default on).                                           |
| `llm-sum/prompts/judge_medhelm_v1.txt` | Default vet-specific judge prompt (MedHELM-style, 5 criteria) — must contain `{REFERENCE_TEXT}` and `{CANDIDATE_SUMMARY}`. |
| `.env`                                | `PHASE3_MODE`, `JUDGE_MODELS` (default `openai,anthropic,gemini`), `JUDGE_PROMPT_FILE`, plus the API key for whatever judges are listed. |

## Outputs

| Path                       | Schema                                                                                         |
|----------------------------|------------------------------------------------------------------------------------------------|
| `data/evaluations.jsonl`   | Append-only. One row per `(doi, summariser, judge, input_source)` tuple: vet-rubric scores, hallucination fields, `requires_human_review`, `parse_method`, token counts, `judge_model_version`, optional `system_fingerprint`, `raw_response_excerpt`, `timestamp`. |
| `data/error_log.jsonl`     | Any judge call that exhausted retries.                                                         |

## CLI

```powershell
python llm-sum/evaluator.py                          # uses PHASE3_MODE from .env
python llm-sum/evaluator.py --mode single            # override
python llm-sum/evaluator.py --judges openai,anthropic  # explicit judge list (2 here)
python llm-sum/evaluator.py --jury                   # full 3-judge panel (the default)
python llm-sum/evaluator.py --no-resume              # re-evaluate everything
python llm-sum/evaluator.py --limit 3                # override mode's paper_limit
```

### Choosing the judges (default is the full 3-judge panel)

The default is the full **3-judge panel** `openai,anthropic,gemini` — evaluation
is a real jury, not a single grader. Dial down for cost. There are four ways to
pick judges, in precedence order (first match wins):

| Control | Example | Result |
|---------|---------|--------|
| `--judges` (CLI) | `--judges openai,anthropic` | Exactly that list. Overrides everything below. |
| `--jury` (CLI) | `--jury` | The full panel `openai,anthropic,gemini`. |
| `JURY_PRESET` (`.env`) | `JURY_PRESET=duo` | `panel` → 3 judges (default); `duo` → `openai,anthropic`; `solo` → single `openai`. |
| `JUDGE_MODELS` (`.env`) | `JUDGE_MODELS=openai` | The list as written (default `openai,anthropic,gemini`). |

`--jury` is just a shortcut for `JURY_PRESET=panel` scoped to one command. When
two or more judges score the data, `eval-report` adds a **Reliability** section
(see [MedHELM-Style Evaluation](medhelm_evaluation.md#reliability-checks)); with
one judge it prints a one-line note explaining why there is no agreement
estimate. Each added judge roughly multiplies judge-call cost by the judge count.

## Choosing the input source: `run_phase3.py evaluate`

`evaluator.py` (this page) always reads `data/summaries.jsonl`, written by
`run_phase3.py summarize`. If you've instead been using `run_phase3.py
summarize-all` — which writes readable `.txt` provider-comparison files to
`data/dev_tests/summaries_txt/` (dev mode) or `data/summaries_txt/` (every
other mode) — use `run_phase3.py evaluate` instead of this script directly.
It accepts an extra `EVAL_INPUT_MODE` setting (`.env`) / `--input-mode` flag
(CLI) that judges those `.txt` files without changing anything about how
judging itself works — same judge calls, same rubric, same parsing:

**`--mode dev` is special:** it defaults to the `dev-jsonl` folder-driven loop
(reads `data/dev_summaries_jsonl/`, writes `data/dev_evals_jsonl/`) **regardless
of `EVAL_INPUT_MODE`**, and skips papers already evaluated there so the dev
sample grows incrementally. See
[dev_evaluation_guide.md](dev_evaluation_guide.md). Pass an explicit
`--input-mode` to override that default for a single run.

```powershell
python llm-sum/run_phase3.py evaluate --mode dev                          # dev-jsonl loop: judge data/dev_summaries_jsonl, write data/dev_evals_jsonl
python llm-sum/run_phase3.py evaluate --mode test  --input-mode dev       # mocked smoke test (dev_tests comparison)
python llm-sum/run_phase3.py evaluate --mode dev   --input-mode jsonl     # override: journal-stratified data/summaries.jsonl
python llm-sum/run_phase3.py evaluate --mode dev   --input-mode dev       # override: judge data/dev_tests/summaries_txt (PHASE3_DEV_LIMIT articles)
python llm-sum/run_phase3.py evaluate --mode batch --input-mode regular   # judge data/summaries_txt (full corpus, once you're ready)
```

| `EVAL_INPUT_MODE` / `--input-mode` | Reads from |
|---|---|
| `jsonl` (default for non-dev modes) | `data/summaries.jsonl` — unchanged original behaviour. |
| `dev-jsonl` (default for `--mode dev`) | `data/dev_summaries_jsonl/` → judges matching `summaries.jsonl` articles → mirrors scores to `data/dev_evals_jsonl/`. Incremental (skips already-judged papers; `--no-resume` forces a full re-judge). |
| `dev` | `data/dev_tests/summaries_txt/` (summarize-all comparison) |
| `regular` | `data/summaries_txt/` |
| `auto` | `dev` when `PHASE3_MODE=dev`, otherwise `regular` — so switching `PHASE3_MODE` also switches the folder. |

Only the processed-text side is ever judged — `summaries_pdf/` (direct-PDF
comparison files) is never read by any input mode. There is no journal-
stratified sampling in `dev`/`regular`/`auto` mode (that machinery assumes
`manifest.jsonl`-backed `summaries.jsonl`); instead every article found in
the folder is judged, capped by the mode's paper limit or `--limit` — for
`dev` mode that is `PHASE3_DEV_LIMIT` articles (default 5), the same cap
`summarize`/`summarize-all` already use, so "how many articles does dev mode
evaluate" is one number everywhere. When the same article has several
timestamped `.txt` runs (`SUMMARIZE_ALL_UNIQUE_OUTPUT=true`), only the most
recent run per article is judged.

## The blind protocol

Non-negotiable. Three structural guarantees:

1. `build_judge_prompt(reference_text, candidate_summary, ...)` — there is **no** `summariser` / `provider` / `model_name` parameter. The function signature cannot leak the identity.
2. The forbidden-token check `BLIND_FORBIDDEN_TOKENS = ("openai", "anthropic", "gemini", "gpt", "claude", "chatgpt")` is asserted against the rendered prompt by `test_blind_judge_prompt_contains_no_model_identifiers` in `tests/test_evaluator.py`.
3. The judge's own model name is logged in `data/evaluations.jsonl` only as `judge_model_version` — never re-injected into a subsequent judge call.

## Parsing the judge response

Tried in order:

1. **Strict JSON** parse of the response body.
2. **First balanced `{...}` block** if the response wraps the JSON in conversational text (e.g. "Sure! Here is..." prefix).
3. **Regex fallback** on `quality_score:`, `hallucination_count:`, etc.
4. **`SCORE_SENTINEL_MALFORMED = 99`** — the row is written with `requires_human_review=True` so a human can adjudicate in Phase 5.

`requires_human_review` also fires when `confidence_score < 3`.

## Behaviour per PHASE3_MODE

| Mode     | What happens                                                                                                       |
|----------|--------------------------------------------------------------------------------------------------------------------|
| `test`   | All judge calls return deterministic mock dicts whose scores are derived from stable SHA-256 hashes of the reference and summary text. No spend. |
| `single` | 1 paper × all summarisers × all judges. Confirm prompt required.                                                   |
| `dev`    | `PHASE3_DEV_LIMIT` papers. Confirm prompt required.                                                                |
| `batch`  | Logs an info note that batch judge results come from `check_batch_status.py`; this script still runs the real-time path for whatever it sees. |

## Common errors and fixes

| Symptom                                                              | Cause                                                       | Fix                                                                                |
|----------------------------------------------------------------------|-------------------------------------------------------------|------------------------------------------------------------------------------------|
| `quality_score = 99` in many rows                                    | Judge keeps returning conversational text instead of JSON.  | Open the file named by `JUDGE_PROMPT_FILE` (default `llm-sum/prompts/judge_medhelm_v1.txt`); tighten the "return JSON only" instruction. |
| `Judge failed after retries`                                         | Network/API issue.                                          | Check `data/error_log.jsonl` for the underlying error; re-run with `--resume`.     |
| Forbidden-token assertion failure in `tests/test_evaluator.py`       | Someone edited the judge prompt to mention a model name.    | Remove the mention; the blind protocol is non-negotiable for study validity.        |

## Worked example

```powershell
$env:PHASE3_MODE = "dev"
$env:PHASE3_DEV_LIMIT = "2"
python llm-sum/evaluator.py
# → [phase3] mode=dev | limit=2 | real-time | confirm-required
# → [phase3:safety] Type 'yes' to confirm: yes
# → [phase3:evaluate] 10.1111/jvim.16872 openai -> openai score=4.0 via json ($0.04)
# → [phase3:evaluate] 10.1111/jvim.16872 anthropic -> openai score=3.85 via json ($0.04)
# → [phase3:evaluate] 10.1111/jvim.16872 gemini -> openai score=4.0 via json ($0.04)
# → [phase3:evaluate] 10.1111/vru.13314  openai -> openai score=3.67 via json ($0.04)
# → ...
# → [phase3:evaluate] done. counts={'evaluated': 6, ...} budget_spent=$0.24
```

`score` here is the row's primary `jury_score` (1–5 scale, MedHELM-style default) —
not the legacy 1–10 `quality_score`.
