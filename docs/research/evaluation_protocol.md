# Evaluation Protocol

## Purpose

This protocol defines how veterinary literature summaries are evaluated. The
primary endpoint is the MedHELM-style `jury_score`, calculated by Python from
criterion-level judge scores.

## Inclusion Rules

- Include papers that have a stable DOI or instance identifier.
- Include only open-access full text or approved processed text artifacts.
- Include summaries with `status=success`.
- Include evaluation rows whose `rubric_version` matches the active protocol.

## Exclusion Rules

- Exclude paywalled content that cannot be accessed through lawful open-access
  sources.
- Exclude rows with missing source text.
- Exclude malformed judge responses from primary analysis unless manually
  adjudicated.
- Do not pool rows across different rubric versions as one endpoint.

## Endpoints

Primary endpoint:

- `jury_score`: weighted 1-5 MedHELM-style score.

Secondary endpoints:

- Hallucination count and hallucination rate.
- Major hallucination rate.
- `requires_human_review` / flagged-for-review rate.
- Automatic metrics such as ROUGE recall, compression ratio, extractive
  coverage, and section coverage.

## Adjudication Policy

Rows should be flagged for manual review when:

- the judge reports a major hallucination,
- `confidence_score < 3`,
- parsing falls back to sentinel values,
- judge disagreement is high in multi-judge mode.

Manual adjudication decisions must record reviewer initials, date, reason, and
whether the row remains in the primary analysis.

## Blind Evaluation Rule

The judge prompt must never include the summarizer model name. Model identity is
stored only after scoring for analysis.
