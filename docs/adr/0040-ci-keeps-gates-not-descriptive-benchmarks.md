# CI keeps gates, not descriptive benchmarks

Status: accepted by the Owner on 2026-11-14 (issue #636). Supersedes two
sentences of [ADR-0034](0034-bounded-upload-execution.md) (its Consequences:
the v0.5.3 baseline measurement, and "Keep existing CI stress sizes"); every
other rule of ADR-0034 stands.

The #562 upload comparison ran on every PR and push as three jobs of 5–8 minutes:
the longest path of CI, with no pass/fail threshold, not required by branch
protection or by the image publisher. Its fixed baseline, v0.5.3, predates the
database rebuilt from scratch (ADR-0035), so the comparison no longer measures
the same schema. The full upload stress (1,024 nested uploads, a 2,048-file
burst) held image publication on `main` for about ten minutes.

## Decision

CI runs only checks that can fail a change. The #562 harness, its tests and its
job are removed rather than kept as a manual workflow: an upload performance
question gets a fresh, purpose-built measurement against a baseline on the
current schema.

The `full` stress profile shrinks to 256 nested uploads and a 512-file burst. It
stays a blocking, exact-SHA publication gate on `main`; the `pr` profile (64),
the throughput floor (0.5 files/s) and the new-connection bound (200) are
unchanged. At 512 files the connection bound still catches a return to one
PostgreSQL connection per DB unit, which would open thousands.

## Consequences

Long-tail effects that need more than 256/512 uploads to appear are no longer
proven before publication. Revisit the sizes if a production run shows such an
effect that CI missed.
