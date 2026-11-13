#!/usr/bin/env python3
"""Rewrite only Filename, preserving all historical metadata bytes; fail closed."""
import collections
import hashlib
from pathlib import Path
import re
import sys

def rewrite(path, base, deb_dir):
    base = base.rstrip('/')
    if not re.fullmatch(r'https://[^/\s?#]+(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*', base):
        raise ValueError('R2 public base must be an HTTPS origin with safe path segments')
    original = Path(path).read_bytes().decode('utf-8', 'surrogateescape')
    expected = collections.Counter((str(p.stat().st_size), hashlib.sha256(p.read_bytes()).hexdigest())
                                   for p in Path(deb_dir).glob('*.deb'))
    actual = collections.Counter()
    blocks = []
    for block in original.split('\n\n'):
        if not block.strip():
            continue
        fields = {}
        for line in block.split('\n'):
            if line and not line[0].isspace() and ': ' in line:
                k, v = line.split(': ', 1)
                if k in fields:
                    raise ValueError('Duplicate metadata field: '+k)
                fields[k] = v
        sha, size = fields.get('SHA256', ''), fields.get('Size', '')
        if not re.fullmatch('[0-9a-f]{64}', sha) or not re.fullmatch('[0-9]+', size) or not fields.get('Filename'):
            raise ValueError('Missing or invalid SHA256/Size/Filename')
        actual[(size, sha)] += 1
        blocks.append(re.sub(r'^Filename: [^\n]*', 'Filename: debs/'+sha+'.deb', block, flags=re.M))
    if not expected or actual != expected:
        raise ValueError('index/deb reconciliation failed')
    Path(path).write_bytes(('\n\n'.join(blocks)+'\n\n').encode('utf-8', 'surrogateescape'))

if __name__ == '__main__':
    rewrite(*sys.argv[1:])
