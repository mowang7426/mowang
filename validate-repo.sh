#!/usr/bin/env bash
# 本地/CI 软件源完整性检查，不修改仓库文件。
set -euo pipefail
cd "$(dirname "$0")"

bash ./check-debs.sh
[ -f Release ] && [ -f Packages ] && [ -f Packages.gz ] && [ -f Packages.xz ]
grep -q '^Architectures: ' Release
grep -q '^SHA256:' Release

python3 <<'PY'
from pathlib import Path

# Parse physical LF records, not Unicode line separators or grep's binary mode.
filenames = [line.removesuffix(b'\r')[10:] for line in Path('Packages').read_bytes().split(b'\n')
             if line.startswith(b'Filename: ')]
if not filenames:
    raise SystemExit('❌ Packages 没有 Filename')
for raw in filenames:
    value = raw.decode('utf-8', 'surrogateescape')
    if value.startswith('https://'):
        if not __import__('re').fullmatch(r'https://[^/]+/debs/[0-9a-f]{64}\.deb', value):
            raise SystemExit(f'❌ 非法远程 Filename：{value}')
        continue
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.is_file():
        raise SystemExit(f'❌ 缺失或非法路径：{path}')
PY

python3 - <<'PY'
from pathlib import Path
import hashlib

r = Path('Release').read_text()
assert 'Architectures:' in r
for name in ['Packages', 'Packages.gz', 'Packages.xz'] + (['Packages.bz2'] if Path('Packages.bz2').exists() else []) + (['Packages.zst'] if Path('Packages.zst').exists() else []):
    data = Path(name).read_bytes()
    for section, fn in [('MD5Sum:', hashlib.md5), ('SHA256:', hashlib.sha256)]:
        start = r.index(section)
        tail = r[start:].split('\n\n', 1)[0]
        row = next((x.strip() for x in tail.splitlines()[1:] if x.strip().endswith(' '+name)), None)
        assert row, f'{section} missing {name}'
        digest, size, path = row.split()
        assert path == name and int(size) == len(data)
        assert digest == fn(data).hexdigest()
print('✓ 软件源完整性检查通过')
PY
