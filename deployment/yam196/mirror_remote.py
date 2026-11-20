"""SSH-stdin deployment operation. Inspect is read-only; apply is explicitly gated."""
from pathlib import Path
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = Path('/home/ai/yam-teleop')
NAMES = ('launch.py', 'python/yam_deploy/runtime.py', 'python/yam_deploy/mirror_test.py',
         'python/yam_deploy/mirror_normal_server.yaml', 'tests/test_mirror_runtime.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def inspect():
    if os.uname().nodename != 'frodobot' or Path.home() != Path('/home/ai'):
        raise RuntimeError('Wrong host/user')
    release = (ROOT / 'current').resolve()
    if release != ROOT / 'releases/yam196-local-r1':
        raise RuntimeError('Release changed')
    sys.path[:0] = [str(release / 'python'), str(release / 'arena-client/yam_client')]
    from yam_deploy import runtime, control
    system = control.System()
    return {'mode': runtime.state(), 'station_sha256': digest(ROOT / 'config/station.json'),
            'files': {n: digest(release / n) for n in (*NAMES, 'manifest.json')},
            'agent': system.ipc('status'),
            'units': {u: system.active(u) for u in control.UNITS + runtime.config()['eval_units']},
            'blockers': system.blockers(include_managed=False)}


def atomic(path, data):
    tmp = path.with_name(path.name + '.mirror-tmp')
    with tmp.open('xb') as stream:
        stream.write(data)
    os.replace(tmp, path)


def apply(payload):
    current = inspect()
    expected = payload['snapshot']
    if current['files'] != expected['files'] or current['station_sha256'] != expected['station_sha256']:
        raise RuntimeError('Reviewed code/configuration changed; nothing written')
    from yam_deploy import control
    if (current['mode'].get('mode') != 'standby' or current['blockers']
            or not all(current['units'][u] for u in control.UNITS)
            or any(v for u, v in current['units'].items() if u not in control.UNITS)):
        raise RuntimeError('Expected healthy exclusive standby')
    a = current['agent']
    if (not a.get('ok') or a.get('teleop_enabled') is not False
            or any(a.get(k) for k in ('active_session_id', 'session_id', 'teleop_session_id', 'home_running', 'arm_stack_running'))):
        raise RuntimeError('Agent is not idle/disabled')
    if set(payload['files']) != set(NAMES):
        raise RuntimeError('Unexpected patch scope')
    release = (ROOT / 'current').resolve()
    backup = ROOT / 'backups' / ('mirror-test-' + time.strftime('%Y%m%d-%H%M%S'))
    with control.transition_lock():
        latest = inspect()
        if (latest['files'] != current['files'] or latest['station_sha256'] != current['station_sha256']
                or latest['mode'] != current['mode'] or latest['units'] != current['units']
                or latest['blockers'] or latest['agent'].get('teleop_enabled') is not False
                or any(latest['agent'].get(k) for k in ('active_session_id', 'session_id', 'teleop_session_id',
                                                       'home_running', 'arm_stack_running'))):
            raise RuntimeError('Standby changed while waiting for deployment lock')
        backup.mkdir(mode=0o700)
        originals = {n: (release / n).read_bytes() if (release / n).exists() else None
                     for n in (*NAMES, 'manifest.json')}
        for n, data in originals.items():
            if data is not None:
                p = backup / n
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        (backup / 'snapshot.json').write_text(json.dumps(current, indent=2))
        try:
            manifest = json.loads(originals['manifest.json'])
            for n, encoded in payload['files'].items():
                data = base64.b64decode(encoded, validate=True)
                atomic(release / n, data)
                manifest['files'][n] = hashlib.sha256(data).hexdigest()
            atomic(release / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
            subprocess.run([str(release / '.venv/bin/python'), '-B', '-m', 'pytest', '-q',
                            str(release / 'tests/test_deployment.py'), str(release / 'tests/test_mirror_runtime.py'),
                            str(release / 'piper-mate/Python_SDK/test_yam_remote_dual_server.py')],
                           cwd=release, check=True, timeout=120)
        except BaseException:
            for n, data in originals.items():
                if data is None:
                    (release / n).unlink(missing_ok=True)
                else:
                    atomic(release / n, data)
            raise
    # Use a fresh process to import the newly deployed guards, not the inspection module cache.
    subprocess.run([str(ROOT / 'bin/teleopctl'), 'mirror-test-start'], check=True, timeout=70)
    print(json.dumps({'backup': str(backup), 'test_capability_enabled': True,
                      'operator_session_started': False, 'Home': 'prohibited'}))


def main():
    payload = json.load(sys.stdin)
    if payload['operation'] == 'inspect':
        print(json.dumps(inspect()))
    elif payload['operation'] == 'apply':
        apply(payload)
    else:
        raise RuntimeError('Unknown operation')


if __name__ == '__main__':
    main()
