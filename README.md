# Veterinary LLM Research Pipeline

**Evaluating GPT-5.5, Claude Opus 4.7, and Gemini 3.1 Pro on veterinary literature summarization**  
*OVC Pet Trust Summer Studentship – 2026*

## Quick Summary
This pipeline processes 250 peer-reviewed veterinary papers (2023–2025) from 5 major journals (JVIM, JAVMA, Vet Surgery, VRU, JFMS). It generates summaries from 3 LLMs, scores them using a **blind LLM-as-a-judge**, and validates the results against human expert scoring (Cohen's Kappa).

## Project Structure
├── data/               # PDFs, text, results (gitignored)
├── src/
│   ├── collect.py      # Build paper manifest via CrossRef API
│   ├── extract.py      # PDF → clean text (remove refs, truncate)
│   ├── summarizer.py   # Call GPT, Claude, Gemini (temp=0)
│   ├── evaluator.py    # Blind judge with structured output
│   ├── pipeline.py     # Resilient orchestrator (resume + atomic writes)
│   ├── human_loop.py   # Export 10% blind sample for manual review
│   ├── stats_engine.py # Cohen's Kappa + covariate analysis
│   └── utils.py        # Budget guard, rate limiter, error logger
└── tests/              # Golden fixture + pytest suite

## Key Features
- **Budget-safe**: Batch API (50% off), hard stop, dry-run mode  
- **Reproducible**: Temperature 0.0, seed 42, logs exact model versions  
- **Scientific**: Blind judge, error ledger, resume after crash  
- **Human validation**: You grade 25 papers → Cohen's Kappa vs. AI judge  

## Summary (CAD)
| Item | Cost |
|------|------|
| APIs (Batch + 2.5× buffer) | ~$220 |
| Tools (Claude Max, 2 months) | ~$280 |
| **Total** | **~$500** | Cost

## Quick Start
```bash
git clone ...
cd veterinary-llm-pipeline
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env   # add your API keys
python src/pipeline.py
