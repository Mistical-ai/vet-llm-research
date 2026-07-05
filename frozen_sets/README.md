# Frozen Benchmark Sets

Frozen sets define the exact papers/instances used for a benchmark run. They
exist so future reruns can answer: "Did we evaluate the same data?"

Each frozen set should have:

- A JSONL file, one instance per line.
- A manifest sidecar with row count and SHA-256 dataset hash.
- Stable `instance_id` and DOI fields.

Before a manuscript-grade run, verify the checksum. The pipeline fails fast if
the frozen set hash does not match the expected value.

`example_medhelm_tiny.jsonl` is a tiny committed fixture for smoke tests only;
it is not a real study dataset.
