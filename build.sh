#!/usr/bin/env bash
# ============================================================
# MoWang 越狱源索引生成器
# 运行：./build.sh
# 需要：dpkg-dev、python3、gzip、bzip2、xz；zstd 可选
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

ORIGIN="莫忘专属源"
LABEL="莫忘专属源"
SUITE="stable"
VERSION="1.0"
CODENAME="iphoneos"
DESCRIPTION="MoWang 越狱源"
ARCHITECTURES="iphoneos-arm iphoneos-arm64 iphoneos-arm64e"

for cmd in dpkg-scanpackages python3 gzip bzip2 xz; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "❌ 缺少依赖：$cmd"
        exit 1
    fi
done

mkdir -p debs

echo "=========================================="
echo "        MoWang 越狱源索引生成器"
echo "=========================================="
echo "源：$LABEL"
echo "支持架构：$ARCHITECTURES"
echo

# 1. 检查所有 deb
if ! bash ./check-debs.sh; then
    echo "❌ deb 检查失败，停止生成索引。"
    exit 1
fi

# 2. 清理旧索引
rm -f Packages Packages.gz Packages.bz2 Packages.xz Packages.zst Release

# 3. 生成 Packages
# -m 保留同一 Package 的多个版本；不同架构的同版本包也会分别保留。
dpkg-scanpackages -m debs > Packages

# 4. 严格检查 Packages 中的 Filename 是否真实存在
python3 <<'PY'
from pathlib import Path

packages = Path("Packages")
text = packages.read_text(encoding="utf-8", errors="replace")
entries = []
current = {}
for line in text.splitlines():
    if not line.strip():
        if current:
            entries.append(current)
        current = {}
        continue
    if ": " in line:
        k, v = line.split(": ", 1)
        current[k] = v
if current:
    entries.append(current)

if not entries:
    raise SystemExit("❌ Packages 为空：debs/ 中没有可索引的 .deb")

allowed = {"iphoneos-arm", "iphoneos-arm64", "iphoneos-arm64e"}
seen = set()
duplicate_keys = set()
for e in entries:
    required = ("Package", "Version", "Architecture", "Filename", "Size", "SHA256")
    missing = [k for k in required if not e.get(k)]
    if missing:
        raise SystemExit(f"❌ Packages 条目缺字段：{missing} / {e}")
    arch = e["Architecture"]
    if arch not in allowed:
        raise SystemExit(f"❌ 发现不支持的架构：{arch} ({e['Package']})")
    filename = e["Filename"]
    if filename.startswith("/") or ".." in Path(filename).parts:
        raise SystemExit(f"❌ 非法 Filename：{filename}")
    if not Path(filename).is_file():
        raise SystemExit(f"❌ Packages 指向不存在的文件：{filename}")
    key = (e["Package"], e["Version"], e["Architecture"])
    if key in seen:
        # 同一 Package/Version/Architecture 的多个文件不属于“多版本”。
        # APT 无法用版本号区分它们，因此这里只警告，不阻止整个仓库生成。
        # 真正的降级支持由 dpkg-scanpackages -m 提供：不同 Version 会全部保留。
        duplicate_keys.add(key)
    seen.add(key)

print(f"✓ Packages 条目：{len(entries)}")
print("✓ 所有 Filename 均存在")
print("✓ 架构：" + ", ".join(sorted({e['Architecture'] for e in entries})))
if duplicate_keys:
    print("⚠ 检测到相同 Package/Version/Architecture 的多个文件（不阻止构建）：")
    for key in sorted(duplicate_keys):
        print("  - " + " / ".join(key))
    print("  提示：真正的降级版本必须使用不同的 Version 字段；-m 已保留多版本。")
PY

# 5. 生成压缩索引
# Sileo/APT 常用 gzip/xz；bz2/zstd 一并保留以兼容不同客户端。
gzip -9c Packages > Packages.gz
bzip2 -9c Packages > Packages.bz2
xz -9c Packages > Packages.xz
if command -v zstd >/dev/null 2>&1; then
    zstd -19 -q -f Packages -o Packages.zst
fi

# 6. 生成 Release，并把所有索引文件的校验值写进去
python3 - <<'PY'
from hashlib import md5, sha256
from pathlib import Path

origin = "莫忘专属源"
label = "莫忘专属源"
suite = "stable"
version = "1.0"
codename = "iphoneos"
description = "MoWang 越狱源"
architectures = "iphoneos-arm iphoneos-arm64 iphoneos-arm64e"

files = ["Packages", "Packages.gz", "Packages.bz2", "Packages.xz"]
if Path("Packages.zst").is_file():
    files.append("Packages.zst")

lines = [
    f"Origin: {origin}",
    f"Label: {label}",
    f"Suite: {suite}",
    f"Version: {version}",
    f"Codename: {codename}",
    f"Architectures: {architectures}",
    "Components: main",
    f"Description: {description}",
    "",
    "MD5Sum:",
]
for name in files:
    data = Path(name).read_bytes()
    lines.append(f" {md5(data).hexdigest()} {len(data)} {name}")
lines += ["", "SHA256:"]
for name in files:
    data = Path(name).read_bytes()
    lines.append(f" {sha256(data).hexdigest()} {len(data)} {name}")
Path("Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

# 7. 最终一致性验证
python3 <<'PY'
from pathlib import Path
import hashlib

release = Path("Release").read_text(encoding="utf-8")
assert "\nArchitectures: " in release, "Release 缺少 Architectures"
assert "\nArchitecture: " not in release, "Release 错误使用单数 Architecture"

sections = {"MD5Sum:": {}, "SHA256:": {}}
section = None
for line in release.splitlines():
    if line in sections:
        section = line
        continue
    if section and line.startswith(" "):
        parts = line.strip().split()
        if len(parts) == 3:
            digest, size, name = parts
            sections[section][name] = (digest, int(size))

files = ["Packages", "Packages.gz", "Packages.bz2", "Packages.xz"]
if Path("Packages.zst").exists():
    files.append("Packages.zst")
for name in files:
    p = Path(name)
    data = p.read_bytes()
    if sections["MD5Sum:"][name] != (hashlib.md5(data).hexdigest(), len(data)):
        raise SystemExit(f"❌ MD5 校验失败：{name}")
    if sections["SHA256:"][name] != (hashlib.sha256(data).hexdigest(), len(data)):
        raise SystemExit(f"❌ SHA256 校验失败：{name}")
    print(f"✓ Release 校验：{name}")

print("✓ Release 使用正确的 Architectures 字段")
print("✓ 所有 Release 校验值正确")
PY

echo
echo "=========================================="
echo "        ✓ 软件源生成完成"
echo "=========================================="
echo
printf '%s\n' "源目录：$(pwd)" "包数量：$(grep -c '^Package: ' Packages)" "根目录文件：" 
ls -lh Packages Packages.gz Packages.bz2 Packages.xz Release 2>/dev/null
[ -f Packages.zst ] && ls -lh Packages.zst
