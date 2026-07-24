# Extending This Pipeline — A Reuse & Handoff Guide

*For the next lab student (or supervisor) who inherits this project and wants to run their own study on top of it.*

The short version:

> The goal was never only this summer's comparison. It was to build a **reproducible pipeline** so that new models, new papers, new rubrics, and new research questions can be plugged in **without rebuilding the methods each time**.

This document is the map for doing that. It does **not** re-explain how the pipeline works or list every command — those already exist:

- **[docs/GUIDE.md](GUIDE.md)** — how the pipeline is built and why (architecture, provenance, safety).
- **[docs/COMMANDS.md](COMMANDS.md)** — every command, in order, copy-paste ready.
- **[.env.template](../.env.template)** — the authoritative, section-numbered catalog of **every** knob (there is a table of contents at the top of the file).

This doc sits on top of those. It answers a different question: *"I have a new study idea — what do I change, and where?"*

---

## 1. The one idea to hold onto: knobs vs. code

Almost everything you would want to vary for a new study is a **config knob**, not a code change. The pipeline was deliberately built so the *methods* stay fixed while the *inputs and settings* move.

There are three levels of change, in increasing effort:

| Level | What it means | Example |
|-------|----------------|---------|
| **1. Change a setting** | Edit a variable in your `.env` file. No code. | Swap to a flagship model, change the judge panel, change the sample size. |
| **2. Small code edit** | Edit one file (a prompt, a rubric, a price row). | Reword the summarization prompt; change the judge rubric weights. |
| **3. Add a component** | Add a new provider or a new corpus. | Add a fourth LLM provider; point the study at different journals. |

You start every study the same way: copy [.env.template](../.env.template) to `.env`, then change **one thing at a time**. The template's top-of-file table of contents tells you which numbered section holds each knob.

> **A quick correction to a common mix-up in the section numbers:** model IDs/tiers are **section 16**, batch + prompt caching is **section 12**, and section 11 is the *summarize-all* (PDF-vs-text) workflow — not models. Reproducibility dials (temperature, seeds) are **section 13**, judge/jury config is **section 14**, and human validation is **sections 18–19**.

---

## 2. The "change a variable per study" map

This is the core of the doc. Find the row that matches your study, change that knob, re-run the pipeline. The **Where** column tells you the file or `.env` section; the **Kind** column tells you whether it's a setting or a code edit.

| I want to study / change… | Change this | Where | Kind |
|---|---|---|---|
| **Try a stronger / flagship model** | `MODEL_TIER=premium` + the `{PROVIDER}_MODEL_PREMIUM` ids, **and** fill the matching price row | [.env.template](../.env.template) §16 + [`_PREMIUM_PRICES`](../llm-sum/models_config.py) in `models_config.py` | Setting + 1 code edit |
| **Which providers write summaries** | `--providers openai,anthropic` on `summarize` | CLI flag | Setting |
| **Which models are judges** | `JUDGE_MODELS=openai,anthropic,gemini` (or `JURY_PRESET=panel/duo/solo`, or `--jury`) | [.env.template](../.env.template) §14 | Setting |
| **How judge scores are combined** | `JURY_AGGREGATION_MODE=unweighted\|weighted`, `JURY_CRITERION_WEIGHTS={...}` | [.env.template](../.env.template) §14; math in [medhelm_evaluation.md](phase3/medhelm_evaluation.md) | Setting |
| **Different journals / years / species** | Rebuild the corpus: [`src/collect.py`](../src/collect.py) → [`src/download.py`](../src/download.py) → [`src/extract.py`](../src/extract.py), then re-run Phase 3 | Phase 2 scripts (see README "Corpus design") | Code + data |
| **The summarization prompt / word budget** | Edit the prompt in [`llm-sum/summarizer.py`](../llm-sum/summarizer.py); the 300–340-word prose budget (hard ceiling 400) governs the judged text | `summarizer.py` | Code edit |
| **The judge rubric / criteria** | Edit [`llm-sum/evaluator.py`](../llm-sum/evaluator.py); or use the free offline heuristic rubric [`docs/rubrics/rubric_v1.yaml`](rubrics/rubric_v1.yaml) via `--use-rubric` (Phase 4) | `evaluator.py` / §17 | Code edit |
| **Input text vs. raw PDF** | `--input-source processed\|raw_text\|pdf` on `summarize` | CLI flag | Setting |
| **Reproducibility dials** | `TEMPERATURE`, `SEED`, and the sample seeds (`PHASE3_DEV_SAMPLE_SEED`, `EVAL_SAMPLE_SEED`, `HUMAN_REVIEW_SEED`, `PUBLICATION_BOOTSTRAP_SEED`) | [.env.template](../.env.template) §13/§15/§18/§20 | Setting |
| **Spend ceiling / run size** | `BUDGET_HARD_STOP` (enforced by `BudgetGuard` in [`src/utils.py`](../src/utils.py)); `PHASE3_MODE=test\|single\|dev\|batch` | [.env.template](../.env.template) §1 / §10 | Setting |
| **More human raters / a pilot first** | `export-human-review` / `ingest-human-review`; pilot twin `export-pilot-human-review` / `ingest-pilot-human-review` | [.env.template](../.env.template) §18–19; [human_validation.md](phase5/human_validation.md) | Setting |

**How the model-tier switch actually resolves** (worth knowing before you swap models). For each provider and role, the first non-empty variable wins:

```text
{PROVIDER}_{SUMMARY|JUDGE}_MODEL_{TIER}  →  {PROVIDER}_MODEL_{TIER}  →  {PROVIDER}_MODEL
```

So an old `.env` that only sets `OPENAI_MODEL` / `ANTHROPIC_MODEL` / `GEMINI_MODEL` keeps working unchanged — those three are the final fallback for every tier and role.

> **The one guard that will stop you:** if you set a `*_MODEL_PREMIUM` id but forget to give it a real price, [`get_model_spec()`](../llm-sum/models_config.py) **raises** rather than run — because a premium price row that still equals the regular row means [`BudgetGuard`](../src/utils.py) would misreport spend. Today `ANTHROPIC_MODEL_PREMIUM=claude-opus-4-8` is live with real prices; the OpenAI and Gemini premium ids are intentionally left blank (intended: `gpt-5.6-sol`, `gemini-3.1-pro-preview`) until their exact ids **and** per-MTok prices are confirmed together. Fill both, or the run fails loudly — which is the point.

---

## 3. Starter project ideas

Ready-to-run studies a new student could pick up. Two are worked out in detail; the rest feed Section 5.

> **Money & safety reminder:** any step that calls `summarize` or `evaluate` for real is a **PAID live-API run**. Per [CLAUDE.md](../CLAUDE.md), those are always run **by a human, manually, in PowerShell** — never automatically. Everything below assumes you run the paid steps yourself.

### Idea A — "Is a flagship model actually worth it?" (small sample, ~20 papers)

**The question.** A full-corpus run on flagship models is expensive. Before spending that, you can ask on a *small* sample: *do flagship models agree with human experts more than the cheaper default lineup — and by enough to justify the cost?*

**The design.** Run the **same** small stratified sample twice — once at the regular tier, once at premium — and correlate each against the **same** human scoresheets.

1. **Pick the sample.** Set `PHASE3_DEV_LIMIT=20` (or pass `--limit`) and use dev mode. `summarize --mode dev` draws a seeded, journal-balanced sample so the same papers are chosen every time.
2. **Run the cheap lineup.** With `MODEL_TIER=regular`:
   ```powershell
   python llm-sum/run_phase3.py summarize --mode dev
   python llm-sum/run_phase3.py evaluate  --mode dev
   ```
   *(Run `evaluate --mode dev` soon after `summarize --mode dev` on a fresh `data/summaries.jsonl`, or pass explicit `--limit`, so it judges exactly the batch you just wrote — see [.env.template](../.env.template) §15 for why the two dev seeds can otherwise diverge.)*
3. **Run the flagship lineup.** Set `MODEL_TIER=premium`, fill the premium ids and their price rows, and repeat step 2.
4. **Get human scores on the same papers.** Export a matching packet and score it in the review UI:
   ```powershell
   python llm-sum/run_phase3.py export-human-review --sample-size 20
   ```
   *(Or rehearse first with the pilot twin — see Idea B.)*
5. **Compare.** Point the report at the human scores and read human-vs-jury agreement for each tier:
   ```powershell
   python llm-sum/run_phase3.py eval-report --human-reviews data/human_reviews.jsonl --human-validation-mode per_reviewer
   ```
   Agreement stats come from [`stats_engine.py`](../llm-sum/stats_engine.py) (Cohen's κ, cost-per-quality) and [`reliability.py`](../llm-sum/reliability.py) (Krippendorff's α); the cost delta comes from `stats-engine`.

**What you get.** A defensible sentence: *"flagship buys **X** more agreement with veterinarian judgment for **Y** more dollars per paper"* — the exact input a supervisor needs to decide whether a full flagship run is worth funding, produced for the price of ~20 papers.

### Idea B — Human-validation overlap design, and a large-scale validation study

Every reviewer gets their own blind packet folder (`humanN/`). The **`--overlap-ratio`** flag (env: `HUMAN_REVIEW_OVERLAP_RATIO`, default `0.6`) controls **how much of the previous reviewer's articles are carried into the next reviewer's packet** — balanced per journal, so the carry never breaks the even per-journal split. This single dial is the heart of any human-validation study design:

| Overlap | What happens | When to choose it |
|---|---|---|
| `--overlap-ratio 0.0` | Every reviewer gets a **fresh, independent** draw — no shared papers | **Maximum coverage.** N reviewers validate ~N× as many summaries. Use when you want the broadest human-validated set and don't need to measure how much reviewers agree with each other. |
| `--overlap-ratio 1.0` | Every reviewer scores the **identical** papers | **Maximum agreement signal.** Cleanest inter-rater reliability (κ/α), least unique coverage. Use for a focused reliability/calibration study, or to onboard/train a new reviewer against a known set. |
| `--overlap-ratio 0.6` (default) | Most papers are unique; a shared core is carried over | **Balanced.** Coverage *and* an agreement anchor. The sensible default for a mixed goal. |

**Scaling this into a large validation study.**

- **Recruit many reviewers and grow packets one at a time.** Each `export-human-review` run adds exactly **one more** `humanN/` folder and never touches earlier ones, so you onboard reviewers incrementally as they become available.
- **Tune sample size to effort.** `--sample-size` must be a multiple of the 5 study journals: `5 → 1/journal`, `10 → 2/journal`, `25 → 5/journal`. Each article expands to ~3 providers' summaries, so a size of 25 is ~75 summaries for one reviewer to read.
- **Match the overlap to the goal.** Want broad coverage across many vets? Lean toward `0.0`. Want a strong "do our experts agree with each other?" claim? Push toward `1.0`. Want both from one study? Keep `0.6` and let the shared core carry the reliability estimate.
- **Report per-reviewer vs. pooled.** `--human-validation-mode per_reviewer` (default) validates each reviewer on their **own** scores — so your supervising vet's agreement is never diluted by averaging with a non-expert. `pooled` averages all reviewers per item; `both` shows both.
- **Rehearse before spending reviewer time.** The pilot twin (`export-pilot-human-review` / `ingest-pilot-human-review`) runs the *entire* reviewer experience against the small dev pool and writes to a **separate ledger** (`data/pilot_human_reviews.jsonl`). The real `ingest-human-review` actively **refuses** to read a pilot folder, so a rehearsal can never contaminate the real results. See [pilot_human_review.md](phase5/pilot_human_review.md).

---

## 4. How reproducible is "reproducible enough"?

Be honest about this — it's the right answer for a lab, and it's the accurate one.

**Strong for *methods* reproducibility.** Same code + same config + same inputs → the same pipeline *behavior*. That's what the fixed seeds (`SEED=42` and friends), `TEMPERATURE=0.0`, append-only JSONL, and the schema contracts buy you.

**Not bit-for-bit-forever**, and that's normal for any LLM study:

1. Providers version their models and change APIs over time.
2. Open-access PDF availability shifts.
3. Exact token/cost numbers can drift as providers update pricing.
4. LLM judges are mildly stochastic at the margins, even at temperature 0, across model versions.

The pipeline handles this the right way — **it records what it ran** rather than pretending nothing changes. Every `evaluate` run writes a manifest to `data/run_manifests/` via [`run_manifest.py`](../llm-sum/run_manifest.py), capturing the `git_commit_sha`, the **observed** `resolved_model_versions`, the `model_tier`, the prompt hash, the seeds, and the temperature. So a future reader can always answer *"what exactly produced this score?"* even after the models have moved on. More on the provenance chain in [booklet chapter 6](booklet/06_data_provenance.md).

The fair sentence for a meeting:

> It's **scientifically reproducible** — versions, seeds, prompts, and costs are all logged — not *frozen in amber forever*. That's the correct standard for an LLM evaluation platform.

---

## 5. What else this platform can host

This is the real payoff of building a platform instead of a script: the same machinery supports many studies. Each row below is a research question you could answer by changing the knob named — not by rebuilding the methods.

| Study | What changes | Mostly a… |
|---|---|---|
| **New model generations** as they ship | Add ids to §16, set `MODEL_TIER` / per-role overrides | Setting |
| **New journals, years, or species** | Rebuild the corpus (Phase 2 scripts) | Data |
| **Prompt or word-budget variations** | Edit the prompt in `summarizer.py` | Code |
| **Rubric / criteria-weighting studies** | `evaluator.py` + `JURY_*` knobs | Code + setting |
| **Judge-agreement / jury-composition studies** | `JUDGE_MODELS` / `JURY_PRESET`, read the Reliability section | Setting |
| **Clinical-strata analyses** | Phase 4 scenarios + the versioned taxonomy (`vet_taxonomy_v1`) | Code (extension point) |
| **Extraction-quality studies** (cleaned text vs. raw PDF) | `summarize-all` (the six-summary comparison) | Setting |
| **Cost-vs-quality tradeoff studies** | `stats-engine` (subscription/paper economics) | Setting |
| **Inter-rater reliability at scale** | Human-validation overlap design (Section 3, Idea B) | Setting |
| **A whole new domain** (human medicine, ecology, any PDF corpus) | New corpus + a domain rubric; everything downstream is unchanged | Code + data |

The intended in-repo extension point for structured new studies is the **Phase 4 scenario/taxonomy scaffolding** — [docs/phase4/README.md](phase4/README.md) describes named, reusable paper-selection rules and a versioned taxonomy with categories (e.g. "Clinical Decision Support," "Client Communication") that are deliberately scaffolded but empty, waiting for the next study to fill them.

---

## 6. End-of-studentship handoff checklist ("freeze a v1 release")

The single biggest gift you can leave the lab is a **clean inheritance point**. Before you leave, do these — they're quick and they turn a strong project into a durable lab asset:

- [ ] **Tag a git release** (e.g. `v1-study`) at the exact commit that produced the study's results.
- [ ] **Pin dependencies** — freeze `requirements.txt` so a future `pip install` reproduces the same library versions (this is why the stats are reproducible; see [statistics_explained.md](statistics_explained.md)).
- [ ] **Write down which `data/` folders are authoritative.** `data/` is git-ignored, so a `git status` will **not** protect it — the authoritative summaries/evaluations/human-review ledgers exist only on disk. State plainly which folders are the real results and which are scratch, and **never bulk-delete a `data/` folder without checking it first**.
- [ ] **Confirm `.env.template` documents every knob** the study actually used, so the next student can recreate your `.env` from the template alone.
- [ ] **Set up shared secrets.** `.env` holds private API keys and is local-only by design (see [CLAUDE.md](../CLAUDE.md)); the lab needs a shared, secure way to hand the next student working keys — not a copied `.env`.

---

## 7. Where to go next

This doc is a hub, not a replacement. For depth:

| For… | Read |
|---|---|
| How the pipeline is built and why | [docs/GUIDE.md](GUIDE.md) |
| Every command, in order | [docs/COMMANDS.md](COMMANDS.md) |
| The Phase 3 engine + full CLI flags | [docs/phase3/README.md](phase3/README.md), [run_phase3.md](phase3/run_phase3.md) |
| The authoritative rubric & jury math | [docs/phase3/medhelm_evaluation.md](phase3/medhelm_evaluation.md) |
| Scenarios, offline rubric, run manifests | [docs/phase4/README.md](phase4/README.md) |
| Human validation & the review UI | [docs/phase5/human_validation.md](phase5/human_validation.md), [review_ui.md](phase5/review_ui.md) |
| Publication tables, figures, stats | [docs/phase6/reporting.md](phase6/reporting.md) |
| Learning the whole thing from scratch | [docs/booklet/BOOKLET.md](booklet/BOOKLET.md) |
| Every configurable knob | [.env.template](../.env.template) |
