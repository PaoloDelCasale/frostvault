# #430 — Essential Owner interface

Base: origin/main bf1e3fd6082c4ff8e662ab6f2815cb6227f23bb7. Separate issue branch/worktree. Draft only; coordinator owns review/merge/issue/epic updates.

Read #430, native parent #410 and closed blockers #423–429; #360, full #365 D1–D17 and #366 resolution/comments, CONTEXT and ADRs 0001–0021. Prototype read via git show, visual B reference only. No prototype code/data enters the application.

Implementation: replace administrative/access shell with browser sidebar and General; explicit request-bound Vault identity (never mutate Session selection to run a search/action); global protected-Vault search; problem links and remedies; essential selection/details/recovery; reuse Stop Protection, Storage Plan/Move now, Recovery Kit and reconnect safeguards. Remove obsolete routes/code/catalog entries/tests together. Full verification in PR CI only; targeted DB-free unit/Vitest locally.

Native relationships verified: parent #410; blockers #423–429 all CLOSED. GitHub token lacks read:project, so project field query unavailable; no project writes attempted.

Progress and final acceptance/check evidence will be recorded here.
