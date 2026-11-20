"""Reviewable upload/install driver. --apply requires the user's explicit approval."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
BUILD = HERE / '.build'
REMOTE_DIR = '/home/ai/yam-teleop-install-20261119'
SSH = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','yam196']


def prepare():
    bundle = BUILD / 'yam196-local-r1.tar.gz'
    deps = BUILD / 'yam196-deps-r1.tar.gz'
    with tarfile.open(deps,'w:gz') as tar:
        tar.add(BUILD / 'deps', arcname='deps')
    hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (bundle,deps)}
    bootstrap = f'''
from pathlib import Path
import hashlib,tarfile,subprocess
stage=Path({REMOTE_DIR!r})
if stage.exists() or Path('/home/ai/yam-teleop').exists():
 raise RuntimeError('Destination exists; refusing overwrite')
hashes={hashes!r}
for name,expected in hashes.items():
 archive=Path('/home/ai')/name
 if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise RuntimeError('Upload checksum mismatch')
stage.mkdir()
for name in hashes:
 with tarfile.open(Path('/home/ai')/name) as tar:
  for member in tar.getmembers():
   if not (stage/member.name).resolve().is_relative_to(stage) or member.issym() or member.islnk() or member.isdev():raise RuntimeError('Unsafe bundle')
  tar.extractall(stage)
subprocess.run(['/usr/bin/python3',str(stage/'yam196-local-r1/install.py'),'--deps',str(stage/'deps'),'--apply'],check=True)
'''
    (BUILD / 'remote-install.py').write_text(bootstrap,encoding='utf-8',newline='\n')
    commands = [
        ['scp','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',str(bundle),'yam196:/home/ai/'+bundle.name],
        ['scp','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',str(deps),'yam196:/home/ai/'+deps.name],
        [*SSH,'python3','-'],
    ]
    review = {'sha256':hashes,'commands':commands,'ssh_stdin':str(BUILD/'remote-install.py'),
              'bootstrap_sha256':hashlib.sha256((BUILD/'remote-install.py').read_bytes()).hexdigest(),
              'writes':[REMOTE_DIR,'/home/ai/yam-teleop','/home/ai/.cache/uv',
                        '/home/ai/.config/systemd/user/yam-teleop-agent.service',
                        '/home/ai/.config/systemd/user/yam-teleop-video@.service',
                        '/home/ai/.config/systemd/user/yam-teleop.target'],
              'starts_services':False,'opens_hardware':False,'remote_network_downloads':False}
    (BUILD/'install-review.json').write_text(json.dumps(review,indent=2)+'\n')
    return commands, bootstrap, review


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.apply:
        review=json.loads((BUILD/'install-review.json').read_text())
        for name, expected in review['sha256'].items():
            if hashlib.sha256((BUILD/name).read_bytes()).hexdigest()!=expected:
                raise RuntimeError('Reviewed artifact changed; review again before upload')
        bootstrap_path=Path(review['ssh_stdin'])
        if hashlib.sha256(bootstrap_path.read_bytes()).hexdigest()!=review['bootstrap_sha256']:
            raise RuntimeError('Reviewed install script changed')
        commands=review['commands'];bootstrap=bootstrap_path.read_text()
    else:
        commands,bootstrap,review=prepare()
    print(json.dumps(review,indent=2),flush=True)
    if args.apply:
        check = "from pathlib import Path; p=[Path('/home/ai/yam-teleop'),Path('/home/ai/yam-teleop-install-20261119'),Path('/home/ai/yam196-local-r1.tar.gz'),Path('/home/ai/yam196-deps-r1.tar.gz')]; assert not any(x.exists() for x in p), 'Destination/upload file exists'"
        subprocess.run([*SSH,'python3','-'],input=check,text=True,check=True)
        for cmd in commands[:2]:subprocess.run(cmd,check=True)
        subprocess.run(commands[2],input=bootstrap,text=True,check=True)
