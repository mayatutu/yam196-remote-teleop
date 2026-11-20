"""Read the authorized target only; save bounded evidence locally (no remote writes)."""
from pathlib import Path
import json
import subprocess

HERE = Path(__file__).resolve().parent
REMOTE = r'''
import pathlib,hashlib,json,subprocess,os
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
root=pathlib.Path('/home/ai/i2rt')
files={str(p.relative_to(root)):digest(p) for p in (root/'i2rt').rglob('*')
       if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
for name in ['pyproject.toml','README.md','LICENSE','uv.lock']:
 p=root/name
 if p.is_file(): files[name]=digest(p)
eval_path=pathlib.Path('/home/ai/colosseum-client/src/colosseum_client/robots/yam.py')
protect=[eval_path,pathlib.Path('/home/ai/colosseum-client/configs/robot.yam-joint-llm.yaml'),
 pathlib.Path('/home/ai/colosseum-unified-ui-yam2/configs/yam2.local.yaml')]
units=['yam2-web.service','yam2-policy.service','yam2-gui.service']
states={}
for unit in units:
 p=pathlib.Path('/home/ai/.config/systemd/user')/unit
 protect.append(p)
 r=subprocess.run(['systemctl','--user','show',unit,'--property=ActiveState,SubState,UnitFileState,MainPID,FragmentPath'],capture_output=True,text=True,check=True)
 states[unit]=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
code='import importlib.metadata as m,json;print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'
pkgs=json.loads(subprocess.run(['/home/ai/colosseum-client/.venv/bin/python','-c',code],capture_output=True,text=True,check=True).stdout)
print(json.dumps({'host':os.uname().nodename,'i2rt_files':files,'protected':{str(p):digest(p) for p in protect},'services':states,'packages':pkgs,'eval_yam_source':eval_path.read_text()}))
'''

if __name__ == '__main__':
    out = HERE / '.build'
    out.mkdir(exist_ok=True)
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             '-o', 'StrictHostKeyChecking=yes', 'yam196', 'python3', '-'],
                            input=REMOTE, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    if data['host'] != 'frodobot':
        raise RuntimeError('Unexpected target')
    source = data.pop('eval_yam_source')
    (out / 'eval_yam.py').write_text(source, encoding='utf-8')
    (out / 'baseline.json').write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print(f'Captured {len(data["i2rt_files"])} driver/model hashes; no target changes')
