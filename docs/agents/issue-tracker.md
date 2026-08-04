# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## When creating an issue

Creating an issue does not require adding it to a GitHub Project. Before
considering it complete:

1. Set its native parent issue when it belongs to an epic.
2. Set native `blocked by` and `blocking` relationships; do not rely only on a
   prose `Depends on:` line.
3. When the issue explicitly belongs to a GitHub Project, add it to that Project
   and assign fields that match adjacent work: initial Status, Phase, Priority,
   and Effort.
4. When it has a parent, place it in the parent's dependency-ordered checklist
   or roadmap section when one exists.
5. Verify the resulting parent and dependencies, plus any applicable project
   item, through `gh` before reporting completion.

Infer applicable field values from the parent and neighboring issues rather
than inventing an isolated classification. If the correct parent or dependency
is ambiguous, ask before creating the issue. Project membership is optional;
if it is unclear, create the issue without adding it to a Project and report
that fact.

## Before working on a referenced issue

Never treat one issue body as a complete, isolated specification.

1. Read the referenced issue, including comments and labels.
2. Resolve its native parent issue and read the parent's full body and comments.
3. Resolve native `blocked by` and `blocking` relationships and inspect every
   directly related issue that can constrain the work.
4. Inspect sibling issues under the same parent when they touch the same domain,
   data model, interface, migration, security rule, or user workflow.
5. Read the relevant project fields and dependency ordering when the issue belongs
   to a GitHub Project.
6. Compare the proposed implementation with those decisions before changing code.

Use GitHub's native issue relationships as the source of truth. The prose
`Depends on:` section is useful context but may be stale. Query parent,
sub-issues, and dependency relationships through `gh api graphql` when `gh issue
view` does not expose them.

If related issues conflict, stop and surface the conflict instead of silently
choosing one interpretation. When a newly agreed decision affects other issues,
update their bodies or relationships in the same work session so future agents
receive a coherent specification.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
