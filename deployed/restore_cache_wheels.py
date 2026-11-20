"""Reconstitute pinned wheels from the target's existing uv cache, without network.

uv's registry cache is keyed by index URL. A wheel present under a previous index
is not found by a new offline resolver using PyPI. Use its unchanged wheel contents
as a local wheelhouse; do not copy or modify the running evaluation environment.
"""
from pathlib import Path
import argparse
import base64
import csv
import email
import hashlib
import io
import importlib.metadata
import json
import re
import shutil
import zipfile


def canonical(name): return re.sub(r'[-_.]+','-',name).lower()


def restore(baseline, dest, cache=None):
    packages=json.loads(baseline.read_text())['packages']
    want={canonical(k):v for k,v in packages.items() if canonical(k) not in ('i2rt','colosseum-client')}
    cache=Path(cache or '/home/ai/.cache/uv')
    roots=[cache/'wheels-v7/pypi',*list((cache/'wheels-v7/index').glob('*'))]
    found={};sources={}
    installed={canonical(d.metadata['Name']): email.message_from_string(d.read_text('WHEEL') or '').get_all('Tag', [])
               for d in importlib.metadata.distributions(path=['/home/ai/colosseum-client/.venv/lib/python3.10/site-packages'])}
    for root in roots:
        if not root.is_dir():continue
        for package in root.iterdir():
            name=canonical(package.name)
            if name not in want or name in found or not package.is_dir():continue
            for wheel in package.iterdir():
                if not wheel.is_dir() or not wheel.name.startswith(want[name]+'-'):continue
                info=list(wheel.glob('*.dist-info/METADATA'))
                if len(info)!=1:continue
                metadata=email.message_from_bytes(info[0].read_bytes())
                if canonical(metadata['Name'])!=name or metadata['Version']!=want[name]:continue
                tags=email.message_from_bytes(info[0].with_name('WHEEL').read_bytes()).get_all('Tag', [])
                matching=[tag for tag in tags if tag in installed.get(name, [])]
                if not matching:continue
                # uv abbreviates long platform tags as a hash in its directory name.
                # Wheel filenames must use the actual PEP 425 tag from WHEEL metadata.
                filename=name.replace('-','_')+'-'+want[name]+'-'+matching[0]+'.whl'
                target=dest/filename
                # Verify recorded archive hashes before regenerating the ZIP container.
                record=info[0].with_name('RECORD')
                for rel,digest,size in csv.reader(io.StringIO(record.read_text())):
                    source=(wheel/rel).resolve()
                    if not source.is_relative_to(wheel.resolve()):raise RuntimeError('Unsafe wheel RECORD')
                    if not source.is_file():raise RuntimeError(f'Incomplete wheel cache: {name}:{rel}')
                    if digest:
                        algorithm,expected=digest.split('=',1)
                        actual=base64.urlsafe_b64encode(hashlib.new(algorithm,source.read_bytes()).digest()).rstrip(b'=').decode()
                        if actual!=expected:raise RuntimeError(f'Corrupt cached wheel: {name}:{rel}')
                with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
                    for source in wheel.rglob('*'):
                        if source.is_file() and '__pycache__' not in source.parts:
                            z.write(source,str(source.relative_to(wheel)))
                found[name]=target.name;sources[target.name]=str(wheel.resolve());break
    for wheel in (cache/'sdists-v10').rglob('*.whl'):
        name=canonical(wheel.name.split('-')[0])
        if name in want and name not in found and wheel.name.split('-')[1]==want[name]:
            shutil.copy2(wheel,dest/wheel.name);found[name]=wheel.name;sources[wheel.name]=str(wheel)
    missing=set(want)-set(found)
    if missing:raise RuntimeError('Required pinned wheels missing: '+','.join(sorted(missing)))
    (dest/'cache-provenance.json').write_text(json.dumps(sources,indent=2)+'\n')
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()
            if p.is_file() and p.name!='manifest.json'}
    (dest/'manifest.json').write_text(json.dumps(hashes,indent=2)+'\n')
    print(json.dumps({'restored_wheels':len(found),'downloaded':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--dest',type=Path,required=True)
    args=p.parse_args();restore(args.baseline,args.dest)
