"""Reviewed SSH-stdin operation; run only through start_standby.py --apply."""
from pathlib import Path
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path, data, mode=0o600):
    temp = path.with_name(path.name + '.standby-tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    os.replace(temp, path)


def idle(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        data = json.load(response)
    return (data.get('run', {}).get('phase') == 'idle'
            and data.get('recording') is False and data.get('preview') is False)


def same_process(pid, expected):
    path = Path('/proc') / str(pid)
    if not path.exists():
        return False
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    start = fields[19]
    if start != expected['starttime']:
        raise RuntimeError(f'Trial process identity changed: {pid}')
    if fields[0] == 'Z':
        return False
    args = (path / 'cmdline').read_bytes().replace(b'\0', b' ').decode()
    if args != expected['args']:
        raise RuntimeError(f'Trial process identity changed: {pid}')
    return True


def main(payload):
    root = Path('/home/ai/yam-teleop')
    release = (root / 'current').resolve()
    if os.uname().nodename != 'frodobot' or Path.home() != Path('/home/ai'):
        raise RuntimeError('Wrong host/user')
    if release != root / 'releases/yam196-local-r1':
        raise RuntimeError('Installed release changed')
    sys.path.insert(0, str(release / 'python'))
    from yam_deploy import runtime, control
    with control.transition_lock():
        if runtime.state()['mode'] not in ('installed', 'eval'):
            raise RuntimeError('Unexpected current deployment mode')
        system = control.System()
        if any(system.active(u) for u in control.UNITS):
            raise RuntimeError('Teleop units are already active')
        station = root / 'config/station.json'
        if digest(station) != payload['station_sha256']:
            raise RuntimeError('Station changed since review')
        cfg = runtime.config()
        if any(system.active(u) for u in cfg['eval_units']) and not idle(cfg['eval_state_url']):
            raise RuntimeError('Evaluation is not idle; no files changed')
        trial = payload['trial']
        for name, expected in trial['hashes'].items():
            if digest(Path(name)) != expected:
                raise RuntimeError('Reviewed trial shutdown implementation changed')
        alive = [int(pid) for pid, spec in trial['processes'].items()
                 if same_process(int(pid), spec)]
        if alive and (4147442 not in alive or not idle('http://127.0.0.1:18772/api/state')):
            raise RuntimeError('Trial is not idle; no files changed')
        if set(system.independent_blockers()) - set(alive):
            raise RuntimeError('Other independent device processes remain')
        if (cfg['enabled'] is not False or any(cfg['commissioning'].values())
                or cfg['server_mapping'] is not None
                or any(a['home'] is not None for a in cfg['arms'].values())):
            raise RuntimeError('Unexpected motion configuration')
        token_file = root / 'config/state.token'
        if token_file.exists():
            raise RuntimeError('Existing credential must not be overwritten')
        if not payload['token'].strip():
            raise RuntimeError('Empty State credential')
        for name, spec in payload['files'].items():
            if digest(release / name) != spec['before']:
                raise RuntimeError(f'Installed file changed: {name}')
        backup = root / 'backups' / payload['revision']
        backup.mkdir(mode=0o700)
        originals = {name: (release / name).read_bytes() for name in payload['files']}
        originals['manifest.json'] = (release / 'manifest.json').read_bytes()
        for name, data in originals.items():
            path = backup / 'release' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (backup / 'station.json').write_bytes(station.read_bytes())
        before_station = station.read_bytes()
        try:
            for name, spec in payload['files'].items():
                data = base64.b64decode(spec['data'], validate=True)
                if hashlib.sha256(data).hexdigest() != spec['after']:
                    raise RuntimeError('Patch checksum mismatch')
                atomic(release / name, data, (release / name).stat().st_mode & 0o777)
            manifest = json.loads(originals['manifest.json'])
            for name, spec in payload['files'].items():
                manifest['files'][name] = spec['after']
            atomic(release / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
            cfg['state_enabled'] = True
            atomic(station, (json.dumps(cfg, indent=2) + '\n').encode())
            atomic(token_file, payload['token'].strip().encode() + b'\n')
            subprocess.run([str(release / '.venv/bin/python'), '-B', '-m', 'pytest', '-q',
                            str(release / 'tests'),
                            str(release / 'piper-mate/Python_SDK/test_yam_remote_dual_server.py')],
                           check=True, timeout=90, cwd=release)
        except BaseException:
            for name, data in originals.items():
                atomic(release / name, data, (release / name).stat().st_mode & 0o777)
            atomic(station, before_station)
            if token_file.exists():
                token_file.unlink()  # Created only by this operation; no preexisting token.
            raise
    print(json.dumps({'patch_installed': True, 'backup': str(backup)}), flush=True)
    # Patch/test failure never touches the running evaluation or trial.
    trial = payload['trial']
    for name, expected in trial['hashes'].items():
        if digest(Path(name)) != expected:
            raise RuntimeError('Reviewed trial shutdown implementation changed')
    parent = 4147442
    processes = trial['processes']
    alive = [int(pid) for pid, spec in processes.items() if same_process(int(pid), spec)]
    if alive:
        if parent not in alive or not idle('http://127.0.0.1:18772/api/state'):
            raise RuntimeError('Trial not idle or parent gone; refusing automatic shutdown')
        if not idle(cfg['eval_state_url']):
            raise RuntimeError('Evaluation is not idle')
        # Uvicorn SIGTERM -> FastAPI lifespan -> Bridge.close -> worker shutdown/EOF.
        os.kill(parent, signal.SIGTERM)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if not any(same_process(int(pid), spec) for pid, spec in processes.items()):
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Trial cleanup still pending; no force-kill or handoff')
    subprocess.run([str(root / 'bin/teleopctl'), 'to-standby'], check=True, timeout=150)
    subprocess.run([str(root / 'bin/teleopctl'), 'status'], check=True, timeout=15)


if __name__ == '__main__':
    main(json.load(sys.stdin))
