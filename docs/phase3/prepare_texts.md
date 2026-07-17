# prepare_texts.py

## What it does

Walks every PDF in `data/raw/` and writes two one-line JSONL cache files:

* `data/raw_text/<descriptive_stem>.jsonl` contains the raw column-aware `pdfplumber` extraction.
* `data/processed/<descriptive_stem>.jsonl` contains the cleaned full-text body after publisher noise and references are removed (folder name configurable via `PROCESSED_DIR_NAME`; ships as `processedv2` by default).

Nothing is truncated at this stage — that happens later inside the summariser when `MAX_INPUT_CHARS` is applied per call.

The on-disk filename mirrors the source PDF stem exactly:
```
data/raw/jvim__title__10_1111_jvim_16872.pdf
data/raw_text/jvim__title__10_1111_jvim_16872.jsonl
data/processed/jvim__title__10_1111_jvim_16872.jsonl
```
So you can glance at `data/processed/` and immediately see which PDF each cache came from. The DOI-derived `slug` is still stored *inside* the JSON body (it's the stable join key for downstream summariser custom_ids).

## When to run it

* After Phase 2 finishes downloading and you have PDFs in `data/raw/`.
* After re-running the audit / quarantine tools — any PDF that's newer than its cache gets re-extracted automatically.
* You can rerun this at any time; it's idempotent (cache freshness check below).

## Inputs

| Path                  | Role                                            |
|-----------------------|-------------------------------------------------|
| `data/raw/*.pdf`      | Source PDFs from Phase 2.                       |
| `data/manifest.jsonl` | Used only to enrich the DOI for each PDF. Optional — orphan PDFs still get cached. |
| `.env`                | `PDFPLUMBER_USE_TEXT_FLOW` (default `true`), `REMOVE_REFERENCES` (default `true`), `MIN_WORD_COUNT_WARN` (default `1000`). |

## Outputs

| Path                                          | Schema                                                          |
|-----------------------------------------------|-----------------------------------------------------------------|
| `data/raw_text/<descriptive_stem>.jsonl`     | Raw extracted text before cleaning, useful for quality/cost comparison. |
| `data/processed/<descriptive_stem>.jsonl`    | Cleaned body text for normal summaries. |

## CLI

```powershell
python llm-sum/prepare_texts.py                       # all PDFs in data/raw/
python llm-sum/prepare_texts.py --limit 5             # first 5 only, smoke test
python llm-sum/prepare_texts.py --manifest <path>     # custom manifest
```

There's no `--mode` flag here — this script does not call any API, so all four modes behave the same.

## Cache-freshness rule

The script skips a PDF when both `data/raw_text/<stem>.jsonl` and `data/processed/<stem>.jsonl` already exist *and* their mtimes are `>=` the PDF mtime. To force a re-extract: delete either cache file or `touch` the PDF.

## Common errors and fixes

| Symptom                                  | Cause                                                | Fix                                                       |
|------------------------------------------|------------------------------------------------------|-----------------------------------------------------------|
| `[prepare] WARNING: <slug> has only N words` | Aggressive reference-section match ate the body, or the PDF really is short. | Run `python scripts/verify_extraction.py` and inspect the FAIL/WARN rows. If references stripping is the cause, set `REMOVE_REFERENCES=false` in `.env` and re-extract just that PDF. |
| `[prepare:extract] FAILED <pdf_name>`    | `pdfplumber` raised; usually an image-only or encrypted PDF. | Open the PDF manually. If it's image-only, either OCR it or remove it from the corpus and rerun Phase 2 supplementation. |
| `<doi>` shows up with `slug = pdf_stem` (legacy-style) in the JSON | The PDF has no matching manifest row, OR the manifest row lacks `journal` / `title`. | Run `python src/audit_raw.py --report` to backfill the manifest, then re-extract. |

## Worked example

```powershell
# After Phase 2:
python llm-sum/prepare_texts.py
# → [phase3:extract] jvim__pre_illness_dietary__10_1111_jvim_16872 (28,431 chars / 4,521 words)
# → ...
# → [phase3:extract] done — extracted=247 cached=3 failed=0

# Audit the result before paying for summaries:
python scripts/verify_extraction.py --quiet
```
