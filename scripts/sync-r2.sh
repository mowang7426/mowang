#!/usr/bin/env bash
# Immutable, additive DEB synchronisation to Cloudflare R2.
set -euo pipefail
: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID is required}"
: "${R2_BUCKET:?R2_BUCKET is required}"
: "${R2_PUBLIC_BASE_URL:?R2_PUBLIC_BASE_URL is required}"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/sync-r2.py"
