#!/usr/bin/env bash
# Mark every older pre-release as superseded by the one just published, so testers never
# download an old build by mistake. Re-running refreshes the notice instead of stacking it.
# Needs GH_TOKEN and GH_REPO (set by the Package workflow's release job).
set -euo pipefail

latest="${1:?usage: supersede_releases.sh <new tag>}"
url="https://github.com/${GH_REPO}/releases/tag/${latest}"
notice="**Superseded by [${latest}](${url}). Please download that version instead.**"

gh release list --limit 100 --json tagName,isPrerelease \
  --jq '.[] | select(.isPrerelease) | .tagName' |
while read -r tag; do
  [ "$tag" = "$latest" ] && continue
  body="$(gh release view "$tag" --json body --jq .body)"
  title="$(gh release view "$tag" --json name --jq .name)"
  # Drop an earlier notice (and the blank line after it), keep the original notes.
  body="$(printf '%s\n' "$body" | awk 'skip && /^$/ {skip=0; next} /^\*\*Superseded by / {skip=1; next} {skip=0; print}')"
  title="${title% (superseded)}"
  printf '%s\n\n%s\n' "$notice" "$body" > "${RUNNER_TEMP:-/tmp}/notes-${tag}.md"
  gh release edit "$tag" --title "${title} (superseded)" --notes-file "${RUNNER_TEMP:-/tmp}/notes-${tag}.md"
  echo "marked ${tag} as superseded by ${latest}"
done
