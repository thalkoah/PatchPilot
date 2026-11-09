#!/usr/bin/env bash
# Stage parent-repository changes without recording example checkouts as gitlinks.
#
# examples/demo_service/.git and examples/minitask/.git are nested repositories.
# git add --all would otherwise store each directory as a gitlink. This script
# removes those two directories, stages the rest of the tree, then recreates
# the checkouts. It does not commit.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

CHECKOUTS=(examples/demo_service examples/minitask)
RESTORED=0

die() {
  echo "add-all: $*" >&2
  exit 1
}

restore_checkouts() {
  [[ $RESTORED -eq 0 ]] || return 0
  RESTORED=1
  echo "recreating example checkouts"
  python3 scripts/init_demo_repo.py
}

trap restore_checkouts EXIT

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git work tree"
top=$(git rev-parse --show-toplevel)
[[ $top == "$ROOT" ]] || die "run this from the PatchPilot repository ($top)"

for checkout in "${CHECKOUTS[@]}"; do
  [[ -d $checkout ]] || die "missing $checkout"
  git_dir=$ROOT/$checkout/.git
  case $git_dir in
    "$ROOT"/examples/*/.git) ;;
    *) die "refusing to remove $git_dir" ;;
  esac
  if [[ -e $git_dir ]]; then
    echo "removing $checkout/.git"
    rm -rf "$git_dir"
  fi
  [[ ! -e $git_dir ]] || die "$checkout/.git still exists"
done

git add --all

if git ls-files -s -- "${CHECKOUTS[@]}" | awk '$1 == "160000" { found = 1 } END { exit found ? 0 : 1 }'; then
  die "a gitlink was staged under examples/. Unstage it with: git rm --cached <path>"
fi

echo "staged. Review with git diff --cached, then commit. Checkouts are recreated when this script exits."
git diff --cached --stat
