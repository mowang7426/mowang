#!/usr/bin/env bash
# Indexes debs/ into Packages, Packages.xz and Release and writes the site
# GitHub Pages serves to _site/. Run by the workflow on every push to
# main; on Debian or Ubuntu it runs the same by hand.
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

for command_name in apt-ftparchive xz; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 69
  fi
done

R2_PUBLIC_BASE_URL="${R2_PUBLIC_BASE_URL:?R2_PUBLIC_BASE_URL is required}"
MAX_PAGES_BYTES="${MAX_PAGES_BYTES:-52428800}"
name="${REPOSITORY_NAME:-$(basename "$repository_root")}"
owner="${REPOSITORY_OWNER:-$name}"
work_dir="$(mktemp -d "$repository_root/.repo-build.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
site_dir="$work_dir/site"
mkdir -p "$site_dir"

shopt -s nullglob
packages=(debs/*.deb)
shopt -u nullglob

# Build metadata from local debs, then replace each Filename with its immutable R2 URL.
if ((${#packages[@]} > 0)); then
  if [[ -s Packages ]]; then
    cp Packages "$work_dir/Packages"
  else
    apt-ftparchive packages debs > "$work_dir/Packages"
  fi
  python3 scripts/index-r2.py "$work_dir/Packages" "$R2_PUBLIC_BASE_URL" debs
else
  : > "$work_dir/Packages"
fi

# Sileo files a package with no Section under nothing; refuse it here.
awk '
  BEGIN { RS = ""; FS = "\n" }
  {
    package = ""; section = ""
    for (field = 1; field <= NF; field++) {
      if ($field ~ /^Package: /) package = substr($field, 10)
      if ($field ~ /^Section: /) section = substr($field, 10)
    }
    if (package != "" && section == "") {
      printf "Warning: package %s has no Section field; keeping it in the index.\n", package > "/dev/stderr"
    }
  }
' "$work_dir/Packages"

gzip -n -9 -c "$work_dir/Packages" > "$work_dir/Packages.gz"
bzip2 -9 -c "$work_dir/Packages" > "$work_dir/Packages.bz2"
xz -9e -c "$work_dir/Packages" > "$work_dir/Packages.xz"
mkdir -p "$work_dir/indexes"
cp "$work_dir/Packages" "$work_dir/Packages.gz" "$work_dir/Packages.bz2" "$work_dir/Packages.xz" "$work_dir/indexes/"

# Release is written beside the indexes, never among them, so it does
# not hash itself.
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="莫忘专属源" \
  -o APT::FTPArchive::Release::Label="莫忘专属源" \
  -o APT::FTPArchive::Release::Suite="stable" \
  -o APT::FTPArchive::Release::Version="1.0" \
  -o APT::FTPArchive::Release::Codename="iphoneos" \
  -o APT::FTPArchive::Release::Architectures="iphoneos-arm iphoneos-arm64 iphoneos-arm64e" \
  -o APT::FTPArchive::Release::Components="main" \
  -o APT::FTPArchive::Release::Description="Packages from $owner" \
  release "$work_dir/indexes" > "$work_dir/Release"

if [[ -d assets ]]; then
  cp -R assets "$site_dir/assets"
fi
for file in index.html CydiaIcon.png sileo-featured.json; do
  if [[ -f "$file" ]]; then
    cp "$file" "$site_dir/"
  fi
done
if [[ -d depictions ]]; then
  cp -R depictions "$site_dir/depictions"
fi
cp "$work_dir/Packages" "$work_dir/Packages.gz" "$work_dir/Packages.bz2" "$work_dir/Packages.xz" "$work_dir/Release" "$site_dir/"
# Stable absolute banner URL at the same R2 origin (Sileo does not resolve relative banners).
python3 - "$site_dir/sileo-featured.json" "$R2_PUBLIC_BASE_URL" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.exists():
    data=json.loads(p.read_text())
    for b in data.get('banners',[]):
        if 'source-banner.jpg' in b.get('url',''):b['url']=sys.argv[2].rstrip('/')+'/assets/source-banner.jpg'
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
PY
# Pages runs no Jekyll over the site.
touch "$site_dir/.nojekyll"
# Never publish a DEB or an oversized Pages artifact.
if [[ -d "$site_dir/debs" ]]; then echo 'Pages artifact must not contain debs/' >&2; exit 1; fi
site_bytes=$(du -sk "$site_dir" | cut -f1)
max_kib=$(( (MAX_PAGES_BYTES + 1023) / 1024 ))
(( site_bytes <= max_kib )) || { echo "Pages artifact too large: ${site_bytes}KiB > ${max_kib}KiB" >&2; exit 1; }
python3 - "$site_dir/Packages" "$R2_PUBLIC_BASE_URL" <<'PY'
from pathlib import Path
import re, sys
rows = [line[10:] for line in Path(sys.argv[1]).read_text(encoding='utf-8', errors='surrogateescape').split('\n') if line.startswith('Filename: ')]
base = sys.argv[2].rstrip('/')
if not rows:
    raise SystemExit('empty Packages')
for value in rows:
    match = re.fullmatch(r'debs/([0-9a-f]{64})\.deb', value)
    if not match or value != f'debs/{match.group(1)}.deb':
        raise SystemExit(f'invalid or non-immutable Package Filename: {value}')
PY

rm -rf -- "$repository_root/_site"
mv "$site_dir" "$repository_root/_site"
echo "Built the APT repository in _site/ (${#packages[@]} packages)"
