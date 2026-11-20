"""Upload a reviewed repair bundle and resume only this unpromoted installation."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

HERE=Path(__file__).resolve().parent
BUILD=HERE/'.build'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','yam196']


def prepare():
    bundle=BUILD/'yam196-repair-r1.tar.gz'
    shutil.copy2(BUILD/'yam196-local-r1.tar.gz',bundle)
    digest=hashlib.sha256(bundle.read_bytes()).hexdigest()
    code=f'''
from pathlib import Path
import hashlib,tarfile,subprocess,os
archive=Path('/home/ai/yam196-repair-r1.tar.gz')
assert hashlib.sha256(archive.read_bytes()).hexdigest()=={digest!r}, 'Repair checksum mismatch'
stage=Path('/home/ai/yam-teleop-install-20261119/revision-2')
assert not stage.exists(), 'Repair stage already exists'
stage.mkdir()
with tarfile.open(archive) as tar:
 for member in tar.getmembers():
  assert (stage/member.name).resolve().is_relative_to(stage) and not (member.issym() or member.islnk() or member.isdev()), 'Unsafe bundle'
 tar.extractall(stage)
os.nice(15)
subprocess.run(['ionice','-c','3','-p',str(os.getpid())],check=True)
release=stage/'yam196-local-r1'
deps=Path('/home/ai/yam-teleop-install-20261119/deps')
subprocess.run(['/usr/bin/python3',str(release/'restore_cache_wheels.py'),'--baseline',str(release/'baseline.json'),'--dest',str(deps)],check=True)
subprocess.run(['/usr/bin/python3',str(release/'install.py'),'--deps',str(deps),'--apply','--resume'],check=True)
'''
    (BUILD/'remote-repair.py').write_text(code,encoding='utf-8',newline='\n')
    report={'bundle_sha256':digest,'script_sha256':hashlib.sha256((BUILD/'remote-repair.py').read_bytes()).hexdigest(),
            'command':['scp','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',str(bundle),'yam196:/home/ai/yam196-repair-r1.tar.gz'],
            'ssh_command':[*SSH,'python3','-'],'ssh_stdin':str(BUILD/'remote-repair.py'),
            'network_downloads':False,'hardware_actions':False}
    (BUILD/'repair-review.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    report=json.loads((BUILD/'repair-review.json').read_text()) if args.apply else prepare()
    print(json.dumps(report,indent=2),flush=True)
    if args.apply:
        assert hashlib.sha256((BUILD/'yam196-repair-r1.tar.gz').read_bytes()).hexdigest()==report['bundle_sha256']
        assert hashlib.sha256(Path(report['ssh_stdin']).read_bytes()).hexdigest()==report['script_sha256']
        check="from pathlib import Path;assert not Path('/home/ai/yam196-repair-r1.tar.gz').exists(),'Repair upload exists'"
        subprocess.run([*SSH,'python3','-'],input=check,text=True,check=True)
        subprocess.run(report['command'],check=True)
        subprocess.run(report['ssh_command'],input=Path(report['ssh_stdin']).read_text(),text=True,check=True)
