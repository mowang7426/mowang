#!/usr/bin/env python3
"""Independent all-record byte audit and real isolated public APT downloads, never install."""
import collections, hashlib, json, os, subprocess, tempfile
from pathlib import Path

def records(raw):
    return [dict(line.split(': ',1) for line in block.split('\n') if ': ' in line and not line.startswith((' ','\t'))) for block in raw.decode('utf-8','surrogateescape').strip('\n').split('\n\n')]

def source_names(manifest, root=Path('.')):
    names=collections.defaultdict(list)
    files=manifest['files']
    if manifest.get('source_of_truth')=='packages/*.json':
        import base64
        records_by_name={p.name:json.loads(p.read_bytes()) for p in (root/'packages').glob('*.json')}
        if set(records_by_name)!=set(files):raise ValueError('manifest record coverage differs from committed catalog')
        for name,key in files.items():
            record=records_by_name[name]
            if record['object_key']!=key:raise ValueError('record/object mapping mismatch')
            names[key].append(base64.b64decode(record['source']['filename_b64']).decode('utf-8','surrogateescape'))
    else:
        for name,key in files.items():names[key].append(name)
    return names


def main():
    original=Path('_original/Packages').read_bytes(); indexed=Path('_site/Packages').read_bytes()
    def without_filename(raw):
        return b'\n'.join(line for line in raw.split(b'\n') if not line.startswith(b'Filename: '))
    if without_filename(original)!=without_filename(indexed):raise ValueError('non-Filename metadata bytes changed')
    rows=records(indexed)
    manifest=json.loads(Path('r2-publication-result.json').read_bytes())
    base=manifest['base'];files=manifest['files']
    triples=collections.Counter((r['Package'],r['Version'],r['Architecture']) for r in rows)
    eligible=[r for r in rows if triples[(r['Package'],r['Version'],r['Architecture'])]==1]
    names=source_names(manifest)
    selected={
        'small':min(eligible,key=lambda r:int(r['Size'])),
        'large':max(eligible,key=lambda r:int(r['Size'])),
        'roothide':next(r for r in eligible if any('roothide' in n.lower() for n in names[r['Filename']])),
        'unicode':next(r for r in eligible if any(any(ord(c)>127 for c in n) for n in names[r['Filename']]))}
    with tempfile.TemporaryDirectory(prefix='r2-real-apt-') as t:
        root=Path(t)
        for d in ['lists/partial','cache/archives/partial','downloads']:(root/d).mkdir(parents=True)
        (root/'sources.list').write_text('deb [trusted=yes] '+base+'/ ./\n')
        (root/'status').touch()
        opts=['-o','Dir::Etc::sourcelist='+str(root/'sources.list'),'-o','Dir::Etc::sourceparts=-','-o','Dir::State::lists='+str(root/'lists'),'-o','Dir::State::status='+str(root/'status'),'-o','Dir::Cache='+str(root/'cache'),'-o','APT::Architecture=iphoneos-arm64e','-o','APT::Architectures::=iphoneos-arm','-o','APT::Architectures::=iphoneos-arm64','-o','APT::Architectures::=iphoneos-arm64e','-o','APT::Sandbox::User=root','-o','Acquire::Retries=5']
        subprocess.run(['apt-get',*opts,'update'],check=True)
        for category,r in selected.items():
            dest=root/'downloads'/category;dest.mkdir()
            package=r['Package']+':'+r['Architecture']+'='+r['Version']
            subprocess.run(['apt-get',*opts,'download',package],cwd=dest,check=True)
            downloads=list(dest.glob('*.deb'))
            if len(downloads)!=1:raise ValueError('APT download count')
            raw=downloads[0].read_bytes()
            if len(raw)!=int(r['Size']) or hashlib.sha256(raw).hexdigest()!=r['SHA256']:raise ValueError('APT downloaded wrong bytes')
            print('REAL PUBLIC APT PASS '+json.dumps({'category':category,'package':package,'size':len(raw),'sha256':r['SHA256']},ensure_ascii=True),flush=True)
    print('ALL RECORDS ORIGINAL METADATA BYTES PASS '+str(len(rows)),flush=True)
if __name__=='__main__':main()
