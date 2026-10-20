#!/usr/bin/env bash
# Posts the `branch-flow` and `in-sync` commit statuses on every open pull request into main and dev.
# Run by .github/workflows/branch-policy.yml, which explains what the two statuses are for.
#
# It only reads the pull requests' commits with git plumbing; it never checks them out or runs them.
# That is what makes it safe to run under pull_request_target.
set -euo pipefail

# Which branches may open a pull request into main and into dev (bash regexes on the head branch).
# Dependabot opens version updates against dev and security updates against main.
INTO_MAIN='^(dev|hotfix/.+|dependabot/.+)$'
INTO_DEV='^((feature|fix|copilot|dependabot)/.+|automation/sync-main-[0-9a-f]+)$'

# `git commit-tree` below needs an identity; the trial merges it makes are never pushed.
export GIT_AUTHOR_NAME=branch-policy GIT_AUTHOR_EMAIL=branch-policy@localhost
export GIT_COMMITTER_NAME=branch-policy GIT_COMMITTER_EMAIL=branch-policy@localhost

run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

post() {  # sha context state description
  echo "  $2: $3 - $4"
  gh api --silent "repos/${GITHUB_REPOSITORY}/statuses/$1" \
    -f context="$2" -f state="$3" -f description="$4" -f target_url="$run_url"
}

# True when merging `from` into `into` would change no file, i.e. `into` already has all of `from`.
# Compares files, not commits: the merge commit every release leaves on main changes no file.
has_all_of() {  # into from
  local tree
  tree=$(git merge-tree --write-tree "$1" "$2" 2>/dev/null) || return 1
  [ "$tree" = "$(git rev-parse "$1^{tree}")" ]
}

judge_flow() {  # base head sha cross_repository
  local base=$1 head=$2 sha=$3 cross=$4
  if [ "$base" = main ]; then
    if [ "$cross" = true ] || ! [[ $head =~ $INTO_MAIN ]]; then
      post "$sha" branch-flow failure "Only dev, hotfix/** and dependabot/** may merge into main"
      return
    fi
    # A hotfix may hold commits of its own and of main, but none that are on dev and not yet released.
    if [ "$head" != dev ] &&
      [ "$(git rev-list --count "$sha" ^origin/main)" != "$(git rev-list --count "$sha" ^origin/main ^origin/dev)" ]; then
      post "$sha" branch-flow failure "Carries unreleased work from dev - branch it from main instead"
      return
    fi
  elif ! [[ $head =~ $INTO_DEV ]]; then
    post "$sha" branch-flow failure "Only feature/**, fix/**, copilot/**, dependabot/** and sync branches may merge into dev"
    return
  fi
  post "$sha" branch-flow success "$head may merge into $base"
}

judge_sync() {  # base head sha
  local base=$1 head=$2 sha=$3 tree merged
  if [ "$base" = main ]; then
    # Then CI, which ran on this branch merged into main, ran on exactly the files that will land.
    if has_all_of "$sha" origin/main; then
      post "$sha" in-sync success "Has every change on main"
    elif [ "$head" = dev ]; then
      post "$sha" in-sync failure "main has changes dev lacks - merge the sync pull request into dev first"
    else
      post "$sha" in-sync failure "main has moved on - update the branch"
    fi
    return
  fi
  # Into dev: would dev, once this is merged, have every change on main? The sync pull request passes;
  # everything else waits for it, so dev cannot drift from main.
  if ! tree=$(git merge-tree --write-tree origin/dev "$sha" 2>/dev/null); then
    post "$sha" in-sync failure "Conflicts with dev - merge dev into the branch first"
    return
  fi
  merged=$(git commit-tree "$tree" -p origin/dev -p "$sha" -m "trial merge" 2>/dev/null)
  if has_all_of "$merged" origin/main; then
    post "$sha" in-sync success "dev will have every change on main"
  else
    post "$sha" in-sync failure "main has changes dev lacks - merge the sync pull request into dev first"
  fi
}

prs=$(for base in main dev; do
  gh pr list --repo "$GITHUB_REPOSITORY" --base "$base" --state open --limit 200 \
    --json number,headRefName,headRefOid,isCrossRepository \
    --jq ".[] | [.number, \"$base\", .headRefName, .headRefOid, .isCrossRepository] | @tsv"
done)

# After a push to main or dev, block merging until the pull requests are judged against it.
if [ "$GITHUB_EVENT_NAME" = push ]; then
  while IFS=$'\t' read -r number base head sha cross; do
    [ -n "$number" ] || continue
    post "$sha" in-sync pending "Re-checking after a push to $GITHUB_REF_NAME"
  done <<<"$prs"
fi

git fetch -q origin main dev
while IFS=$'\t' read -r number base head sha cross; do
  [ -n "$number" ] || continue
  echo "#$number $head -> $base"
  if ! git fetch -q origin "+refs/pull/$number/head:refs/pr/$number"; then
    echo "  could not fetch it; closed since it was listed?"
    continue
  fi
  if [ "$(git rev-parse "refs/pr/$number")" != "$sha" ]; then
    echo "  moved since it was listed; the run its push started will judge it"
    continue
  fi
  judge_flow "$base" "$head" "$sha" "$cross"
  judge_sync "$base" "$head" "$sha"
done <<<"$prs"
