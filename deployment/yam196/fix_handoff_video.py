"""Prepare offline; --apply fixes the camera/CAN false positive and resets test capability."""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

from prepare_mirror_test import SSH

HERE = Path(__file__).resolve().parent

REMOTE = r'''
import base64,hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path('/home/ai/yam-teleop');release=(root/'current').resolve()
if os.uname().nodename!='frodobot' or release!=root/'releases/yam196-local-r1':
    raise RuntimeError('Wrong host/release')
sys.path[:0]=[str(release/'python'),str(release/'arena-client/yam_client')]
from yam_deploy import runtime,control
p=json.load(sys.stdin);s=control.System()
with control.transition_lock():
    a=s.ipc('status')
    if (runtime.state().get('mode')!='mirror-test' or not a.get('ok')
        or any(a.get(k) for k in ('active_session_id','session_id','teleop_session_id','arm_stack_running','home_running'))
        or s.blockers(include_managed=False)
        or any(s.active(u) for u in runtime.config()['eval_units'])):
        raise RuntimeError('Requires idle mirror-test with no real arm owners')
    manifest_path=release/'manifest.json';manifest=json.loads(manifest_path.read_text())
    for n,v in p['files'].items():
        path=release/n
        old=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if old!=v['before'] or (old is not None and manifest['files'].get(n)!=old):
            raise RuntimeError('Reviewed source changed: '+n)
    backup=root/'backups'/('handoff-video-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
    names=[*p['files'],'manifest.json']
    originals={n:(release/n).read_bytes() if (release/n).exists() else None for n in names}
    for n,data in originals.items():
        if data is not None:
            path=backup/n;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    def write(path,data):
        tmp=path.with_name(path.name+'.handoff-tmp')
        with tmp.open('xb') as f:f.write(data)
        os.replace(tmp,path)
    try:
        for n,v in p['files'].items():
            data=base64.b64decode(v['data'],validate=True);write(release/n,data)
            manifest['files'][n]=hashlib.sha256(data).hexdigest()
        write(manifest_path,(json.dumps(manifest,indent=2)+'\n').encode())
        subprocess.run([str(release/'.venv/bin/python'),'-B','-m','pytest','-q',
            str(release/'tests/test_handoff_video.py'),str(release/'tests/test_deployment.py'),
            str(release/'tests/test_mirror_runtime.py'),str(release/'piper-mate/Python_SDK/test_yam_remote_dual_server.py')],
            cwd=release,check=True,timeout=120)
    except BaseException:
        for n,data in originals.items():
            if data is None:(release/n).unlink(missing_ok=True)
            else:write(release/n,data)
        raise
subprocess.run([str(root/'bin/teleopctl'),'mirror-test-stop'],check=True,timeout=70)
subprocess.run([str(root/'bin/teleopctl'),'mirror-test-start'],check=True,timeout=70)
print(json.dumps({'backup':str(backup),'fixed':'camera senders excluded only from CAN handoff',
    'test_capability_enabled':True,'arm_session_started':False}))
'''


def payload():
    after_control = (HERE / 'yam_deploy/control.py').read_text(encoding='utf-8')
    before_control = after_control.replace('include_managed=False, include_video=True', 'include_managed=False')
    new = """                patterns = ('yam_remote_dual_server', 'remote_teleop.home_move', 'arm_relay_server_bridge')
                if include_video:
                    patterns += ('low-latency-teleop/sender',)
                if include_managed and any(x in args for x in patterns):"""
    old = """                if include_managed and any(x in args for x in ('yam_remote_dual_server',
                        'remote_teleop.home_move', 'arm_relay_server_bridge', 'low-latency-teleop/sender')):"""
    if before_control.count(new) != 1:
        raise RuntimeError('Unexpected control adapter')
    before_control = before_control.replace(new, old)
    after_handoff = (HERE / '.build/release/arena-client/yam_client/remote_teleop/can_handoff.py').read_text(encoding='utf-8')
    before_handoff = after_handoff.replace('include_managed=True, include_video=False', 'include_managed=True')
    tests = (HERE / 'tests/test_deployment.py').read_text(encoding='utf-8')
    node = next(n for n in ast.parse(tests).body if isinstance(n, ast.FunctionDef)
                and n.name == 'test_can_handoff_allows_owned_video_but_blocks_every_arm_owner')
    test = 'import pytest\nfrom yam_deploy import runtime,control\nfrom test_deployment import station,commission\n\n' + ast.get_source_segment(tests, node) + '\n'
    files = {}
    for name, before, after in (
        ('python/yam_deploy/control.py', before_control, after_control),
        ('arena-client/yam_client/remote_teleop/can_handoff.py', before_handoff, after_handoff),
        ('tests/test_handoff_video.py', None, test)):
        data = after.encode()
        files[name] = {'before': hashlib.sha256(before.encode()).hexdigest() if before is not None else None,
                       'data': base64.b64encode(data).decode()}
    return {'files': files}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    value = payload()
    dest = HERE / '.build/windows-mirror-test/handoff-fix-review.json'
    dest.write_text(json.dumps(value, indent=2), encoding='utf-8')
    if not args.apply:
        print(json.dumps({'review': str(dest), 'files': list(value['files']), 'connected': False}))
        return
    loader = 'import base64;exec(compile(base64.b64decode(' + repr(base64.b64encode(REMOTE.encode()).decode()) + "),'<handoff-fix>','exec'))"
    r = subprocess.run([*SSH, 'python3 -c ' + shlex.quote(loader)], input=json.dumps(value), text=True)
    if r.returncode:
        raise RuntimeError('Handoff repair failed; inspect output; no automatic arm-session start')


if __name__ == '__main__':
    main()
