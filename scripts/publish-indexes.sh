#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

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

# Never depend on another path-filtered run taking over: even a README-only
# commit can advance main. Rebuild the newest snapshot here, at most 3 times.
# Rebuilds use our own temporary worktree; the caller's checkout is not reset.
rebuild_dir=''
require_advance() {
  if [[ "$remote" == "$base" ]] || ! git merge-base --is-ancestor "$base" "$remote"; then
    echo 'ERROR: remote main is not a strict descendant of the build base.' >&2
    exit 1
  fi
}

for attempt in 1 2 3; do
  read_remote
  if [[ "$remote" != "$base" ]]; then
    require_advance
    echo "REBUILD: main advanced ($base -> $remote), attempt $attempt/3."
    if [[ -z "$rebuild_dir" ]]; then
      rebuild_dir=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/mowang-indexes.XXXXXX")/checkout
      git worktree add --detach "$rebuild_dir" "$remote"
      cd "$rebuild_dir"
    else
      # Previous generated files have been committed locally; never discard edits.
      [[ -z $(git status --porcelain) ]] || { echo 'ERROR: rebuild worktree is dirty.' >&2; exit 1; }
      git checkout --detach "$remote"
    fi
    base=$remote
    bash ./build.sh
    bash ./validate-repo.sh
  fi

  git add -- Packages Packages.gz Packages.bz2 Packages.xz Packages.zst Release
  changed=false
  if ! git diff --cached --quiet; then
    git -c user.name='github-actions[bot]' \
        -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
        commit -m 'build: regenerate repository indexes'
    changed=true
  fi

  # Recheck even when generation is a no-op; an upload may have arrived meanwhile.
  read_remote
  if [[ "$remote" != "$base" ]]; then
    require_advance
    echo "RETRY: main advanced before push, attempt $attempt/3."
    continue
  fi
  if [[ "$changed" == false ]]; then
    echo "No generated changes to commit at $base."
    exit 0
  fi

  if output=$(git push --porcelain origin HEAD:refs/heads/main 2>&1); then
    printf '%s\n' "$output"
    echo "PUBLISHED: indexes built at $base (attempt $attempt/3)."
    exit 0
  else
    status=$?
    printf '%s\n' "$output" >&2
    # Only a porcelain non-fast-forward AND proven remote advancement is retryable.
    # Auth/network/hook failures remain errors, even if main advanced concurrently.
    case "$output" in
      *'[rejected] (fetch first)'*|*'[rejected] (non-fast-forward)'*)
        read_remote
        require_advance
        echo "RETRY: main advanced during push, attempt $attempt/3."
        continue
        ;;
    esac
    echo 'ERROR: publication failed without a confirmed non-fast-forward race.' >&2
    exit "$status"
  fi
done
echo 'ERROR: main kept advancing; publication exhausted 3 attempts.' >&2
exit 1
