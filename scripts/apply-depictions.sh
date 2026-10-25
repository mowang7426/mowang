#!/usr/bin/env bash
# Read Packages from stdin; attach repository depictions without repacking debs.
set -euo pipefail
awk '
  BEGIN { RS = ""; FS = "\n"; ORS = "\n\n" }
  {
    target = 0
    for (i = 1; i <= NF; i++)
      if ($i == "Package: org.shallowdream.dockpages") target = 1
    if (!target) { print $0; next }
    entry = ""
    for (i = 1; i <= NF; i++) {
      if ($i ~ /^(Depiction|SileoDepiction):/) continue
      entry = entry (entry == "" ? "" : "\n") $i
    }
    base = "https://mowang7426.github.io/mowang/depictions/org.shallowdream.dockpages/"
    print entry "\nDepiction: " base "index.html\nSileoDepiction: " base "depiction.json"
  }
'
