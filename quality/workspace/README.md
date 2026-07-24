# Quality Playbook workspace (v1.5.6 Phase 6 layout)

Intermediate pipeline artifacts live here after Phase 6 so top-level `quality/`
is dominated by canonical deliverables (`REQUIREMENTS.md`, `BUGS.md`, etc.).

Subdirectories: `code_reviews/`, `spec_audits/`, `patches/`, `writeups/`,
`mechanical/`, `results/`.

Top-level symlinks (`quality/results` -> `workspace/results`, etc.) keep
historical path references and the mandatory `bash quality/mechanical/verify.sh`
entrypoint valid. `quality_gate.py` resolves both layouts.
