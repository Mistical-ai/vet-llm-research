# verify_extraction.py

## What it does

Audits `data/raw/*.pdf` against the cleaned cache in `data/processed/*.jsonl`. For each PDF it extracts the *raw* pdfplumber text (no cleaning), compares the word count against the cached cleaned word count, and assigns a PASS / WARN / FAIL status. No API calls; safe to re-run; read-only.

Healthy papers land at ratio 0.70-0.90 (references + publisher noise removed = ~15-25% of raw text). The thresholds are conservative — a tight 0.90 rule would flag every paper.

## When to run it

* Before a `single`/`dev`/`batch` summarisation run — catches bad extractions before you pay for summaries of mangled text.
* After re-running `prepare_texts.py` to confirm nothing regressed.
* Whenever a summary comes back nonsensical and you want to check the input text quality.

## Inputs

| Path                       | Role                                       |
|----------------------------|--------------------------------------------|
| `data/raw/*.pdf`           | Source PDFs.                               |
| `data/processed/*.jsonl`   | Cleaned cache files written by `prepare_texts.py`. |
| `data/manifest.jsonl`      | Used to match PDF → cache filename.        |

## Outputs

| Path                                  | Content                                                                 |
|---------------------------------------|-------------------------------------------------------------------------|
| stdout                                | Aligned table with one row per PDF.                                     |
| `data/verify_extraction_report.txt`   | Same table plus a per-status summary; overwritten on each run.          |

## CLI

```powershell
python scripts/verify_extraction.py                # full audit
python scripts/verify_extraction.py --limit 10     # first 10 PDFs only
python scripts/verify_extraction.py --quiet        # only print WARN/FAIL rows
```

## Status thresholds

| Status | Rule                                                              | What it means                                              |
|--------|-------------------------------------------------------------------|------------------------------------------------------------|
| `PASS` | ratio ≥ 0.50 AND cleaned_words ≥ 3000                             | Cleaning behaved as expected.                              |
| `WARN` | ratio 0.30-0.49, OR cleaned_words 1500-2999                       | Aggressive cleaning or a genuinely short paper; eyeball it.|
| `FAIL` | ratio < 0.30 OR cleaned_words < 1500 OR cache missing OR raw_words < 500 | Broken: image-only PDF, eaten body text, or pipeline never ran. |

Exit code: `0` if zero FAILs; `1` if any FAIL (so CI / scripted runs can gate on it).

## Common errors and fixes

| Symptom                                                     | Cause                                                              | Fix                                                                                              |
|-------------------------------------------------------------|--------------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| FAIL with `no cache`                                        | `prepare_texts.py` has not run for this PDF.                       | `python llm-sum/run_phase3.py extract`.                                                          |
| FAIL with `raw words ... < 500`                             | The PDF is image-only or has no extractable text.                  | OCR the file, or remove it from `data/raw/` and rerun Phase 2 supplementation.                   |
| FAIL with `ratio < 0.30 (cleaning ate the body?)`           | The "References" header pattern matched something in the discussion. | Set `REMOVE_REFERENCES=false` in `.env`, delete the cache file, re-extract just that PDF.        |
| FAIL with `pdfplumber could not open`                       | Corrupt / encrypted / unreadable PDF.                              | Try opening it manually; if broken, replace via Phase 2 supplementation.                         |
| Lots of WARN rows                                           | Reference stripping is borderline on this corpus.                  | Spot-check a couple; if they look fine, ignore.                                                  |

## Worked example

```powershell
PS> python scripts/verify_extraction.py --quiet
PDF filename                                                  pages      raw    clean  ratio  status
------------------------------------------------------------------------------------------------------
vru__short_communication__10_1111_vru_13322.pdf                   3     1820     1107   0.61  WARN  (ratio 0.61, cleaned 1107 words)
javma__editorial_response__10_2460_javma_99001.pdf                2      612      298   0.49  FAIL  (cleaned words 298 < 1500)

Summary: PASS=243  WARN=6  FAIL=1  (total 250)

Report written to data/verify_extraction_report.txt
```

The FAIL row above is an editorial — it should have been filtered by Phase 2's article-type gate. Running `python src/audit_article_types.py --remove` would move it to `data/quarantine/`.
