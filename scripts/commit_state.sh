#!/usr/bin/env bash
# Commit queue/ready/posted/failed/status/logs if the poster changed them.
set -euo pipefail

cd "$(dirname "$0")/.."

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add queue ready posted failed status logs

if git diff --cached --quiet; then
  echo "No poster state to commit."
  exit 0
fi

git commit -m "${1:-chore: update clip poster state}"

branch="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"
git pull --rebase origin "$branch"
git push origin "HEAD:${branch}"
