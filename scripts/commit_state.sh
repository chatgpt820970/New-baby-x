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
# Retry: other workflows (prefetch, manual queue pushes) may commit at the same time.
# On conflicts in status/logs, keep this run's version (-X theirs = the commit being replayed).
for attempt in 1 2 3 4 5; do
  if git pull --rebase -X theirs origin "$branch" && git push origin "HEAD:${branch}"; then
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  echo "commit_state: attempt ${attempt} failed, retrying" >&2
  sleep $((attempt * 5))
done
echo "commit_state: could not push poster state" >&2
exit 1
