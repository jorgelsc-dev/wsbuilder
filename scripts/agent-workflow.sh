#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/agent-workflow.sh prepare <type> <slug>
  scripts/agent-workflow.sh pr

Commands:
  prepare   Sync main from origin/main when possible and create a fresh topic branch.
  pr        Push the current topic branch and open or reuse a PR against main.

Allowed branch types:
  feat, fix, docs, chore, refactor, test, perf
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

gh_bin() {
  printf '%s' "${GH_BIN:-gh}"
}

run_gh() {
  "$(gh_bin)" "$@"
}

require_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repository"
}

require_origin() {
  git remote get-url origin >/dev/null 2>&1 || die "remote 'origin' is not configured"
}

current_branch() {
  git branch --show-current
}

is_dirty() {
  [[ -n "$(git status --porcelain)" ]]
}

slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's#[^a-z0-9._-]+#-#g; s#-+#-#g; s#(^-+|-+$)##g'
}

validate_type() {
  case "$1" in
    feat|fix|docs|chore|refactor|test|perf) ;;
    *)
      die "invalid branch type '$1'. Use one of: feat, fix, docs, chore, refactor, test, perf"
      ;;
  esac
}

set_merge_base() {
  local branch="$1"
  git config "branch.${branch}.gh-merge-base" main
}

prepare_branch() {
  local type="${1:-}"
  local raw_slug="${2:-}"
  local slug branch branch_now

  [[ -n "$type" && -n "$raw_slug" ]] || die "prepare requires <type> and <slug>"
  validate_type "$type"

  slug="$(slugify "$raw_slug")"
  [[ -n "$slug" ]] || die "branch slug cannot be empty after normalization"

  branch="${type}/${slug}"
  branch_now="$(current_branch)"

  if git rev-parse --verify "$branch" >/dev/null 2>&1; then
    die "branch '$branch' already exists"
  fi

  if [[ "$branch_now" == "main" ]]; then
    if is_dirty; then
      info "main has local changes; creating '$branch' from current HEAD without syncing origin/main"
      git switch -c "$branch"
      set_merge_base "$branch"
      info "prepared branch '$branch'"
      return
    fi

    require_origin
    info "syncing main from origin/main"
    git fetch origin main --prune
    git switch main
    git pull --ff-only origin main
    git switch -c "$branch"
    set_merge_base "$branch"
    info "prepared branch '$branch'"
    return
  fi

  if is_dirty; then
    die "current branch '$branch_now' has local changes; commit, stash, or finish that branch before starting another task"
  fi

  require_origin
  info "switching back to main and syncing origin/main before creating '$branch'"
  git switch main
  git fetch origin main --prune
  git pull --ff-only origin main
  git switch -c "$branch"
  set_merge_base "$branch"
  info "prepared branch '$branch'"
}

create_or_show_pr() {
  local branch

  branch="$(current_branch)"
  [[ -n "$branch" ]] || die "unable to determine current branch"
  [[ "$branch" != "main" ]] || die "refusing to open a PR from main"

  require_origin
  set_merge_base "$branch"

  info "pushing '$branch' to origin"
  git push -u origin "$branch"

  command -v "$(gh_bin)" >/dev/null 2>&1 || die "gh CLI is not installed. Run: gh pr create --base main --head '$branch' --fill"

  if ! run_gh auth status >/dev/null 2>&1; then
    die "gh CLI is not authenticated. Run 'gh auth login' and then: gh pr create --base main --head '$branch' --fill"
  fi

  if run_gh pr view "$branch" --json url --jq '.url' >/dev/null 2>&1; then
    info "PR already exists:"
    run_gh pr view "$branch" --json url --jq '.url'
    return
  fi

  run_gh pr create --base main --head "$branch" --fill
}

main() {
  require_repo

  case "${1:-}" in
    prepare)
      shift
      prepare_branch "${1:-}" "${2:-}"
      ;;
    pr)
      shift
      create_or_show_pr
      ;;
    -h|--help|help|"")
      usage
      ;;
    *)
      die "unknown command '$1'"
      ;;
  esac
}

main "$@"
