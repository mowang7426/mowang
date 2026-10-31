#!/usr/bin/env bash
set -euo pipefail

# Non-main manual runs may validate, but must never publish into main.
if [[ "${INDEX_BUILD_REF:?INDEX_BUILD_REF is required}" != refs/heads/main ]]; then
  echo "SKIP: validation-only run for $INDEX_BUILD_REF; main is unchanged."
  exit 0
fi
base=${INDEX_BASE_SHA:?INDEX_BASE_SHA is required}
if [[ $(git rev-parse HEAD) != "$base" ]]; then
  echo 'ERROR: HEAD differs from the recorded build base.' >&2
  exit 1
fi
if ! git diff --cached --quiet; then
  echo 'ERROR: unexpected staged changes before index publication.' >&2
  exit 1
fi

read_remote() {
  # Fetch is read-only for the checkout: never pull, merge or rebase indexes.
  git fetch --no-tags origin refs/heads/main
  remote=$(git rev-parse FETCH_HEAD)
}

read_remote
if [[ "$remote" != "$base" ]]; then
  if git merge-base --is-ancestor "$base" "$remote"; then
    echo "SKIP: stale indexes built at $base; main advanced to $remote."
    exit 0
  fi
  echo 'ERROR: remote main is not a descendant of the build base.' >&2
  exit 1
fi

git add -- Packages Packages.gz Packages.bz2 Packages.xz Packages.zst Release
if git diff --cached --quiet; then
  echo 'No generated changes to commit.'
  exit 0
fi
git -c user.name='github-actions[bot]' \
    -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
    commit -m 'build: regenerate repository indexes'

# Porcelain identifies a ref race separately from auth, network and hook failures.
# A failed push is skipped ONLY for a non-fast-forward rejection AND a proven
# descendant of our base on main. No force push and no retry of obsolete indexes.
if output=$(git push --porcelain origin HEAD:refs/heads/main 2>&1); then
  printf '%s\n' "$output"
  echo "PUBLISHED: indexes built at $base."
else
  status=$?
  printf '%s\n' "$output" >&2
  case "$output" in
    *'[rejected] (fetch first)'*|*'[rejected] (non-fast-forward)'*)
      read_remote
      if [[ "$remote" != "$base" ]] && git merge-base --is-ancestor "$base" "$remote"; then
        echo "SKIP: main advanced during publication ($base -> $remote); obsolete indexes were not pushed."
        exit 0
      fi
      ;;
  esac
  echo 'ERROR: publication failed without a confirmed non-fast-forward race.' >&2
  exit "$status"
fi
