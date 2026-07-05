# Threats To Validity

## Judge Bias

LLM judges may prefer certain writing styles or share training-data overlap with
some summarizer families. The blind protocol reduces model-name bias, but it
does not remove all judge bias. Multi-judge runs and manual adjudication help
measure this risk.

## Extraction Bias

PDF extraction can lose tables, captions, or multi-column ordering. Because the
judge scores summaries against extracted text, extraction errors can look like
summary errors or hide true hallucinations.

## Dataset Bias

Open-access availability is not random. The corpus may overrepresent journals,
species, clinical topics, or study designs that publish more open-access full
text.

## Model Drift

Hosted model providers can update models behind stable names. The pipeline logs
model versions and fingerprints where available, but old runs may not be exactly
repeatable if providers retire versions.

## Rubric Drift

Scores from different rubric versions are not directly comparable. Reports must
filter or stratify by `rubric_version`.

## Human Review Bias

Manual adjudication can introduce reviewer subjectivity. Review decisions should
be documented with reasons and, when feasible, checked by a second reviewer.
