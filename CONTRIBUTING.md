# Contributing

## Branch Naming

Use short, descriptive branch names with a slash-delimited prefix:

- `phase/<number>-<topic>` for planned research phases, for example `phase/4-medhelm`
- `fix/<short-topic>` for bug fixes
- `docs/<short-topic>` for documentation-only changes
- `ci/<short-topic>` for workflow and tooling changes

The older branch name `phase4_medhelm` remains valid as a historical branch, but
new work should use `phase/4-medhelm` style names. Keeping a predictable branch
pattern makes GitHub Actions, pull requests, and research audit notes easier to
read months later.

## Commit Messages

Use an imperative, concise subject line:

```text
Add frozen-set checksum validation.
```

When the change affects research methods, add a body that explains why the
change matters for reproducibility, safety, or validity.

## Safety Rules

- Do not commit `.env`, API keys, raw credentials, downloaded PDFs, or generated
  paid-run outputs.
- Do not run live API modes in CI. CI must use `PHASE3_MODE=test` and
  `DRY_RUN=true`.
- Do not force-push shared branches unless explicitly coordinated.
- Do not compare scores from different rubric versions as if they were the same
  endpoint.

## Pull Request Checklist

- Unit tests pass with `python -m pytest tests -q`.
- Any research-method change updates `docs/research/` or the relevant
  `docs/phase3/` guide.
- New run outputs are written under `runs/<run_id>/` or ignored local `data/`,
  not committed as ad hoc files.
- New dependencies are reflected in the requirements input and lock files.
