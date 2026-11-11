# #551 implementation scope

- Isolated branch `perf/upload-provenance-shared-lock`; coordinator alone merges/releases.
- Read #551 including owner comment, native relationships (no parent/dependencies), #535, #458, #464, #410 and adjacent #549/#550/#552/#554. Project metadata unavailable to current token (`read:project` absent).
- Add explicit audited `shared=True` mode (`FOR SHARE`, never `KEY SHARE`); default remains exclusive. Propagate through nested target checks.
- Audit DB-only binding/read, snapshot/digest/reuse, linkage, prepare/send-started, verification metadata. Keep receipt/version publication and final verification exclusive, including Local Copy observation and aggregate/event mutations.
- Preserve Vault-first order, durable dirty marks, pre-send committed intent, exact-version retries with no repeated provider write, pause at claim boundary.
- Test SQL recordings of opted-in paths, PG concurrency/exclusive writer fences, pause/cancel safety and retry/exact-version behavior. Preserve stress sizes, floors and gates.
- No production access, real cloud, system package installation, backups, service/config/data changes, merge, release or delegation.
- Local Python 3.12 `.venv`; no PG16 server binaries found on PATH or standard Debian path. Database/full validation goes to PR CI; targeted non-DB tests locally.
