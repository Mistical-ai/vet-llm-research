# evaluator.py

## What it does

Reads every `(paper, summariser)` pair from `data/summaries.jsonl` and asks a **judge model** to score the summary against the original cleaned text. Records quality (1-10), hallucination count, hallucination categories, confidence (1-5), and a `requires_human_review` flag for the Phase-5 sample. The judge is **blind to the summariser identity** — the prompt template only sees `{REFERENCE_TEXT}` and `{CANDIDATE_SUMMARY}`.

Real-time only. Batch evaluation submissions are handled by `batch_utils.py` alongside summarisation, and the results are collected by `check_batch_status.py`.

## When to run it

* After `data/summaries.jsonl` has at least one row with `status=success`.
* In `single`/`dev` mode whenever you change the judge prompt at `llm-sum/prompts/judge_v1.txt`.

## Inputs

| Path                                  | Role                                                                       |
|---------------------------------------|----------------------------------------------------------------------------|
| `data/summaries.jsonl`                | Source of (paper, summariser, summary) triples.                            |
| `data/processed/*.jsonl`              | Cleaned reference text the judge compares against.                         |
| `data/evaluations.jsonl` (if exists)  | Used by `--resume` (default on).                                           |
| `llm-sum/prompts/judge_v1.txt`        | Judge prompt — must contain `{REFERENCE_TEXT}` and `{CANDIDATE_SUMMARY}`.  |
| `.env`                                | `PHASE3_MODE`, `JUDGE_MODELS` (default `openai`), `JUDGE_PROMPT_FILE`, plus the API key for whatever judges are listed. |

## Outputs

| Path                       | Schema                                                                                         |
|----------------------------|------------------------------------------------------------------------------------------------|
| `data/evaluations.jsonl`   | Append-only. One row per `(doi, summariser, judge)` triple: `quality_score`, `hallucination_count`, `hallucination_categories`, `confidence_score`, `requires_human_review`, `parse_method`, `reasoning`, `input_tokens`, `output_tokens`, `judge_model_version`, `raw_response_excerpt`, `timestamp`. |
| `data/error_log.jsonl`     | Any judge call that exhausted retries.                                                         |

## CLI

```powershell
python llm-sum/evaluator.py                          # uses PHASE3_MODE from .env
python llm-sum/evaluator.py --mode single            # override
python llm-sum/evaluator.py --judges openai,anthropic  # multi-judge
python llm-sum/evaluator.py --no-resume              # re-evaluate everything
python llm-sum/evaluator.py --limit 3                # override mode's paper_limit
```

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
| `test`   | All judge calls return deterministic mock dicts whose `quality_score` is hash-derived from the summary text. No spend. |
| `single` | 1 paper × all summarisers × all judges. Confirm prompt required.                                                   |
| `dev`    | `PHASE3_DEV_LIMIT` papers. Confirm prompt required.                                                                |
| `batch`  | Logs an info note that batch judge results come from `check_batch_status.py`; this script still runs the real-time path for whatever it sees. |

## Common errors and fixes

| Symptom                                                              | Cause                                                       | Fix                                                                                |
|----------------------------------------------------------------------|-------------------------------------------------------------|------------------------------------------------------------------------------------|
| `quality_score = 99` in many rows                                    | Judge keeps returning conversational text instead of JSON.  | Open `llm-sum/prompts/judge_v1.txt`; tighten the "return JSON only" instruction.   |
| `Judge failed after retries`                                         | Network/API issue.                                          | Check `data/error_log.jsonl` for the underlying error; re-run with `--resume`.     |
| Forbidden-token assertion failure in `tests/test_evaluator.py`       | Someone edited the judge prompt to mention a model name.    | Remove the mention; the blind protocol is non-negotiable for study validity.        |

## Worked example

```powershell
$env:PHASE3_MODE = "dev"
$env:PHASE3_DEV_LIMIT = "2"
python llm-sum/evaluator.py
# → [phase3] mode=dev | limit=2 | real-time | confirm-required
# → [phase3:safety] Type 'yes' to confirm: yes
# → [phase3:evaluate] 10.1111/jvim.16872 openai -> openai score=8 via json ($0.04)
# → [phase3:evaluate] 10.1111/jvim.16872 anthropic -> openai score=7 via json ($0.04)
# → [phase3:evaluate] 10.1111/jvim.16872 gemini -> openai score=8 via json ($0.04)
# → [phase3:evaluate] 10.1111/vru.13314  openai -> openai score=9 via json ($0.04)
# → ...
# → [phase3:evaluate] done. counts={'evaluated': 6, ...} budget_spent=$0.24
```
