#!/usr/bin/env bash
# Keep one tracking issue per audit in sync with the latest non-PR audit run.
#
#   ci_audit_issue.sh <audit-name> <success|failure|...> <gate-log-file>
#
# Needs GH_TOKEN (issues: write) and GH_REPO. Pull-request runs never call this:
# they use baseline mode and must not create noise. On failure the single open
# issue titled "Security audit failing on main: <audit-name>" is created or its
# body refreshed (no comment spam); once the audit passes it is closed.
set -euo pipefail

audit="${1:?audit name}"
outcome="${2:?step outcome}"
log="${3:?gate log file}"
title="Security audit failing on main: ${audit}"
run_url="${GITHUB_SERVER_URL:-https://github.com}/${GH_REPO}/actions/runs/${GITHUB_RUN_ID:-0}"

existing="$(gh issue list --repo "$GH_REPO" --state open --search "\"$title\" in:title" \
  --json number,title --jq "[.[] | select(.title == \"$title\")][0].number // empty")"

if [[ "$outcome" == "success" ]]; then
  if [[ -n "$existing" ]]; then
    gh issue close "$existing" --repo "$GH_REPO" --reason completed \
      --comment "The ${audit} audit passes again on main (${run_url})."
  fi
  exit 0
fi

# Anything other than a clean pass or a skipped gate is reported.
[[ "$outcome" == "skipped" ]] && exit 0

body="$(mktemp)"
{
  echo "The scheduled/main **${audit}** audit is failing. This does **not** block pull requests: they only fail on findings they introduce (baseline mode)."
  echo
  echo "Latest run: ${run_url}"
  echo
  echo '```'
  tail -n 80 "$log" 2>/dev/null || echo "(no gate output captured)"
  echo '```'
  echo
  echo "Fix with a targeted lockfile/override bump, or add a dated exception (see docs/ci.md). This issue closes itself when the audit passes again."
} >"$body"

if [[ -n "$existing" ]]; then
  gh issue edit "$existing" --repo "$GH_REPO" --body-file "$body"
else
  gh issue create --repo "$GH_REPO" --title "$title" --body-file "$body" --label dependencies
fi
