#!/usr/bin/env bash
# Real APT relative Filename update/download; isolated lists/cache, no installation.
set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo/debs" "$tmp/pkg/DEBIAN" "$tmp/lists/partial" "$tmp/cache/archives/partial" "$tmp/download"
printf 'Package: test.r2.relative\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nMaintainer: Test <test@example.org>\nDescription: inert APT fixture\nSection: Tweaks\n' > "$tmp/pkg/DEBIAN/control"
dpkg-deb --build "$tmp/pkg" "$tmp/original.deb"
sha=$(sha256sum "$tmp/original.deb" | cut -d' ' -f1)
cp "$tmp/original.deb" "$tmp/repo/debs/$sha.deb"
(cd "$tmp/repo"; apt-ftparchive packages debs > Packages)
R2_PUBLIC_BASE_URL=https://example.r2.dev python3 scripts/index-r2.py "$tmp/repo/Packages" https://example.r2.dev "$tmp/repo/debs"
printf 'deb [trusted=yes] file:%s/repo ./\n' "$tmp" > "$tmp/sources.list"
opts=(-o "Dir::Etc::sourcelist=$tmp/sources.list" -o Dir::Etc::sourceparts=- -o "Dir::State::lists=$tmp/lists" -o "Dir::Cache=$tmp/cache" -o APT::Architecture=iphoneos-arm64e -o APT::Get::List-Cleanup=0 -o APT::Sandbox::User=root)
apt-get "${opts[@]}" update
(cd "$tmp/download"; apt-get "${opts[@]}" download test.r2.relative)
cmp "$tmp/original.deb" "$tmp/download/"*.deb
echo "APT relative Filename download byte/hash verified: $sha"
