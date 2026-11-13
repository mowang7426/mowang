#!/usr/bin/env bash
# Immutable, additive DEB synchronisation to Cloudflare R2.
set -euo pipefail
: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID is required}"
: "${R2_BUCKET:?R2_BUCKET is required}"
: "${R2_PUBLIC_BASE_URL:?R2_PUBLIC_BASE_URL is required}"
command -v aws >/dev/null || { echo 'aws CLI is required' >&2; exit 69; }
shopt -s nullglob
packages=(debs/*.deb)
((${#packages[@]})) || { echo 'No deb packages found' >&2; exit 1; }
endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
for deb in "${packages[@]}"; do
  sha=$(sha256sum -- "$deb" | cut -d' ' -f1)
  size=$(stat -c '%s' -- "$deb")
  key="debs/${sha}.deb"
  echo "Checking ${deb##*/} -> ${key} (${size} bytes)"
  existing=$(aws s3api head-object --endpoint-url "$endpoint" --bucket "$R2_BUCKET" --key "$key" --query '[ContentLength,Metadata.sha256]' --output text 2>/dev/null || true)
  if [[ -n "$existing" && "$existing" != "None"$'\tNone' ]]; then
    read -r old_size old_sha <<<"$existing"
    [[ "$old_size" == "$size" && "$old_sha" == "$sha" ]] || { echo "Refusing different existing object: $key" >&2; exit 1; }
  else
    # If another writer wins after HEAD, the conditional put fails closed.
    if ! aws s3api put-object --endpoint-url "$endpoint" --bucket "$R2_BUCKET" --key "$key" --body "$deb" --metadata "sha256=$sha,size=$size" --if-none-match '*' >/dev/null; then
      echo "Upload race or R2 error for $key; refusing overwrite" >&2; exit 1
    fi
  fi
  read -r got_size got_sha <<<"$(aws s3api head-object --endpoint-url "$endpoint" --bucket "$R2_BUCKET" --key "$key" --query '[ContentLength,Metadata.sha256]' --output text)"
  [[ "$got_size" == "$size" && "$got_sha" == "$sha" ]] || { echo "R2 HEAD verification failed: $key" >&2; exit 1; }
  tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
  aws s3api get-object --endpoint-url "$endpoint" --bucket "$R2_BUCKET" --key "$key" "$tmp" >/dev/null
  [[ "$(sha256sum "$tmp" | cut -d' ' -f1)" == "$sha" && "$(stat -c '%s' "$tmp")" == "$size" ]] || { echo "R2 GET verification failed: $key" >&2; exit 1; }
  rm -f "$tmp"
done
echo "R2 coverage verified: ${#packages[@]} objects"
