# cost_estimator.py

## What it does

Forecasts the USD cost of a summarisation + evaluation run **without making any API calls**. By default it reads cached cleaned text from `data/processed/*.jsonl`; with `--input-source raw_text` through `run_phase3.py`, it reads `data/raw_text/*.jsonl` so you can compare how much extra it would cost to send raw extracted PDF text. It tokenises with `tiktoken` (or falls back to `words × 1.33` if tiktoken is missing), and applies the per-million-token prices from [`llm-sum/models_config.py`](../../llm-sum/models_config.py).

Direct PDF input is different: `--input-source pdf` sends the actual PDF file to each provider's PDF ingestion path, so this offline estimator cannot reliably predict the token count. For PDF comparisons, use `--mode single --input-source pdf`; the summarizer records real token counts and cost from the provider responses.

The forecast respects the current `PHASE3_MODE`: in `batch` mode the per-provider batch discount is applied where supported.

## When to run it

* Before any `single`/`dev`/`batch` run, to compare projected cost against `BUDGET_HARD_STOP`.
* After changing `MAX_INPUT_CHARS` or `MAX_OUTPUT_TOKENS` in `.env`.
* After adding/removing providers or judges.

## Inputs

| Path                       | Role                                                                    |
|----------------------------|-------------------------------------------------------------------------|
| `data/processed/*.jsonl`   | Cached cleaned text; iterated for token counts.                         |
| `data/raw_text/*.jsonl`    | Optional raw extracted text when estimating with `--input-source raw_text`. |
| `data/raw/*.pdf`           | Not estimated offline; direct PDF token counts come from live provider responses. |
| `llm-sum/models_config.py` | Prices per million tokens, batch discount flags.                        |
| `.env`                     | `BUDGET_HARD_STOP`, `MAX_OUTPUT_TOKENS`, `JUDGE_MODELS`.                 |

## Outputs

Stdout only — no files are written. (If you want the report saved, redirect to a file.)

## CLI

This module has **no standalone CLI entry point** — it has no `if __name__ == "__main__":` block, so `python llm-sum/cost_estimator.py` runs the file and produces no observable output. It is always invoked indirectly: either imported (`from cost_estimator import run`) or reached through `run_phase3.py summarize --estimate`, which calls `run()` for you:

```powershell
python llm-sum/run_phase3.py summarize --estimate
python llm-sum/run_phase3.py summarize --estimate --input-source raw_text
# Not supported: python llm-sum/run_phase3.py summarize --estimate --input-source pdf
```

## Behaviour per PHASE3_MODE

| Mode     | Batch discount in the forecast?                                            |
|----------|-----------------------------------------------------------------------------|
| `test`   | Real-time prices.                                                          |
| `single` | Real-time prices (1 paper × 3 providers).                                  |
| `dev`    | Real-time prices, full corpus tokens but obviously you'd cap papers at run time. |
| `batch`  | Batch prices for providers where `supports_batch=True` (OpenAI, Anthropic). Gemini stays real-time. |

## Model tiers and roles

Prices and model IDs come from `models_config.get_model_spec(provider, role="summarize"|"judge")`. The summarisation leg calls it with `role="summarize"` (the default) and the judge leg with `role="judge"`, so the two can resolve to different models even for the same provider. Each resolves independently through this chain, first match wins:

```
{PROVIDER}_{SUMMARY|JUDGE}_MODEL_{TIER} → {PROVIDER}_MODEL_{TIER} → {PROVIDER}_MODEL
```

`MODEL_TIER` (`.env.template` section 16, default `regular`) picks which tier's env vars get consulted first — set it to `premium` for a sensitivity run on a stronger/pricier lineup (e.g. `ANTHROPIC_MODEL_PREMIUM=claude-opus-4-8`) without touching the regular-tier vars everything else depends on. A blank premium model ID falls back to the regular tier with a printed warning rather than failing. The forecast above reflects whichever tier and role resolve for the run's active `.env`.

## Common errors and fixes

| Symptom                                                              | Cause                                              | Fix                                                              |
|----------------------------------------------------------------------|----------------------------------------------------|------------------------------------------------------------------|
| `No cached texts in data/processed/` or `data/raw_text/`             | `prepare_texts.py` has not run, or raw caches predate this feature. | `python llm-sum/run_phase3.py extract`.                          |
| `Direct PDF cost cannot be estimated offline`                        | PDF token counts are provider-specific and only known after a live call. | Run `python llm-sum/run_phase3.py summarize --mode single --input-source pdf`. |
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
