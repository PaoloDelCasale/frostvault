#!/usr/bin/env bash
# Publication policy: the incompatible 0.6+ line never advances latest
# implicitly. A separately requested, exact-SHA-gated promotion may do so.
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo 'usage: image_tags.sh IMAGE FULL_SHA GIT_REF' >&2
  exit 64
fi
image="$1"
sha="$2"
ref="$3"
if [[ ! "$image" =~ ^[a-z0-9][a-z0-9./:_-]*$ || ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'invalid image name or full commit SHA' >&2
  exit 64
fi

tags=("${image}:sha-${sha:0:7}")
case "$ref" in
  refs/heads/*)
    ;; # Main/manual builds have only an immutable commit tag.
  refs/tags/v*)
    version="${ref#refs/tags/v}"
    if [[ ! "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
      echo 'release tag must be a full stable semantic version' >&2
      exit 64
    fi
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    tags+=("${image}:${version}" "${image}:${major}.${minor}")
    # Preserve the old line's historical policy without upgrading installations
    # across the new catalog baseline. Future lines are opt-in as well.
    if [[ "$major" == 0 && "$minor" -lt 6 ]]; then
      tags+=("${image}:latest")
    fi
    ;;
  *)
    echo 'unsupported publication ref' >&2
    exit 64
    ;;
esac
printf '%s\n' "${tags[@]}"
