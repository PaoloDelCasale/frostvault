# Excluded is a protection state, not pending work

Issue #498: a Vault from a Mac folder never became protected. Files the
automatic upload never admits (`._*`, `.DS_Store`, `Thumbs.db`, `*.lock`,
`*-wal`, `*-journal`, plus the Vault's own include/exclude globs) fell through
the protection CASE to `pending` forever, while the upload activity and the
delay monitoring (which already apply those rules) correctly saw no pending
work. The Vault aggregate was therefore `pending` with nothing to wait for.

## Decision

The shared protection vocabulary gains a sixth observation, **Excluded**
(`excluded`): a present Local Copy that is not Protected, has no problem, and
whose path the automatic-upload rules exclude (fixed noise exclusions and the
Vault's `include_globs`/`exclude_globs`, the same rules as admission).

- Order is unchanged for every existing state; `excluded` is assigned only where
  the file would otherwise be `pending`. A failed upload, Cloud-only Conflict,
  missing or mismatching copy, or unknown availability still wins, so exclusion
  never hides a problem. An excluded file that is verified in the cloud (for
  example by an explicit manual upload) is simply `protected`.
- Excluded files never count against a Vault or folder: the aggregate state is
  the worst of problem, unconfirmed, pending, protected, cloud_only and is
  `excluded` only when nothing else is present.
- Excluded means "not expected to be in the cloud", not "safe". A Vault can be
  `protected` while an excluded file holds content that was never uploaded, or
  that changed after a verified upload and was excluded later (a new glob, a
  rename into an excluded name). The file's older verified copy, if any, stays
  indicated (`previous_snapshot_protected`, and `last_known_state` when
  availability is unknown), but its current bytes are not protected. Users who
  need such content protected must not exclude it; the Details text says so.
- Manual operations stay explicit and unchanged. An excluded file keeps its
  upload action in the API (`upload_eligible`); only the automatic path ignores
  it. The Details page offers no new button for it (ADR-0016's guarantees about
  cloud content are untouched).
- The state is derived at read time from the Vault's current policy. The SQL
  embeds the path rules as literals (one source in `upload_path_rules`, shared
  with upload activity). Directory rollups are a disposable projection: migration
  0066 adds `state_excluded` and marks them for rebuild, and changing a Vault's
  include/exclude globs requests a rebuild. No catalog evidence, Job or cloud
  content is written.
- The rules are read through one translation (`upload_path_rules`): Python's
  glob regex rewritten as a POSIX ARE both engines read alike (set contents
  escaped, so `[:`, `[.` and `[=` never open a class). Paths are compared after
  the same backslash-to-separator normalization admission applies, so a Linux
  name containing `\` is classified like admission does. Saving a glob that has
  no safe translation (NUL, an empty range such as `[z-a]`) is refused with 422;
  an already stored one is treated as matching nothing and never blocks other
  edits or breaks a query. Global aggregate maintenance isolates each Vault in a
  savepoint, so one failing Vault cannot stall the others or the worker tick.
- The UI shows Excluded as its own label and filter, never as Pending, with a
  short explanation in file Details. `GET /api/stats` reports it in `states` and
  never in `protection_state` unless it is the only observation.

## Considered options

- Hiding excluded files from the catalog or stats: loses the explanation for why
  a file has no cloud copy and breaks counts versus browsing.
- A persisted per-file exclusion flag: needs recomputation on every glob change
  and can disagree with admission; reading the current policy cannot.
- Fixed noise only: leaves a Vault with an exclude glob pending forever, the
  same defect for user rules.
