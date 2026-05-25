# cost_estimator.py

## What it does

Forecasts the USD cost of a full summarisation + evaluation run **without making any API calls**. Reads cached cleaned text from `data/processed/*.jsonl`, tokenises it with `tiktoken` (or falls back to `words × 1.33` if tiktoken is missing), and applies the per-million-token prices from [`llm-sum/models_config.py`](../../llm-sum/models_config.py).

The forecast respects the current `PHASE3_MODE`: in `batch` mode the per-provider batch discount is applied where supported.

## When to run it

* Before any `single`/`dev`/`batch` run, to compare projected cost against `BUDGET_HARD_STOP`.
* After changing `MAX_INPUT_CHARS` or `MAX_OUTPUT_TOKENS` in `.env`.
* After adding/removing providers or judges.

## Inputs

| Path                       | Role                                                                    |
|----------------------------|-------------------------------------------------------------------------|
| `data/processed/*.jsonl`   | Cached cleaned text; iterated for token counts.                         |
| `llm-sum/models_config.py` | Prices per million tokens, batch discount flags.                        |
| `.env`                     | `BUDGET_HARD_STOP`, `MAX_OUTPUT_TOKENS`, `JUDGE_MODELS`.                 |

## Outputs

Stdout only — no files are written. (If you want the report saved, redirect to a file.)

## CLI

This script is normally invoked indirectly:

```powershell
python llm-sum/run_phase3.py summarize --estimate
```

You can also call it directly:

```powershell
python llm-sum/cost_estimator.py     # works if you import + invoke its run() function
```

## Behaviour per PHASE3_MODE

| Mode     | Batch discount in the forecast?                                            |
|----------|-----------------------------------------------------------------------------|
| `test`   | Real-time prices.                                                          |
| `single` | Real-time prices (1 paper × 3 providers).                                  |
| `dev`    | Real-time prices, full corpus tokens but obviously you'd cap papers at run time. |
| `batch`  | Batch prices for providers where `supports_batch=True` (OpenAI, Anthropic). Gemini stays real-time. |

## Common errors and fixes

| Symptom                                                              | Cause                                              | Fix                                                              |
|----------------------------------------------------------------------|----------------------------------------------------|------------------------------------------------------------------|
| `No cached texts in data/processed/`                                 | `prepare_texts.py` has not run.                    | `python llm-sum/run_phase3.py extract`.                          |
| Token counts look ~5-10% off                                         | Fallback to `words × 1.33` because tiktoken isn't installed. | `pip install tiktoken` (already in `requirements.txt`).      |
| Forecast says `OVER BUDGET`                                          | Real cost would exceed `BUDGET_HARD_STOP`.         | Either raise the budget in `.env` or reduce paper count via `--mode dev` / `--limit`. |

## Worked example

```powershell
python llm-sum/run_phase3.py summarize --estimate
# → [phase3] mode=batch | batch-API | confirm-required
# → [phase3:estimate] Cost forecast for 247 paper(s)
# →   Mode: BATCH (50% off where supported)
# →   Output tokens per summary: 500
# →
# →       openai (batched)      avg_in=4521tok  avg=$0.0223/paper  total=$5.51
# →    anthropic (batched)      avg_in=4521tok  avg=$0.0213/paper  total=$5.25
# →       gemini (real-time)    avg_in=4612tok  avg=$0.0165/paper  total=$4.08
# →
# →   Summarisation subtotal : $14.84
# →   Evaluation (judge)     : $10.69
# →   GRAND TOTAL            : $25.53
# →   Budget hard stop       : $50.00  [WITHIN BUDGET, margin $24.47]
```
