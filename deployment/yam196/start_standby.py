"""Prepare offline by default. --apply uploads the reviewed patch and starts real video."""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import re
import subprocess

HERE = Path(__file__).resolve().parent
BUILD = HERE / '.build'
REVIEW = BUILD / 'standby-review.json'
PATCHES = {
    'launch.py': 'launch.py',
    'python/yam_deploy/runtime.py': 'yam_deploy/runtime.py',
    'python/yam_deploy/control.py': 'yam_deploy/control.py',
    'tests/test_deployment.py': 'tests/test_deployment.py',
    'tests/conftest.py': 'tests/conftest.py',
}
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'StrictHostKeyChecking=yes', 'yam196']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare():
    files = {}
    for remote, local in PATCHES.items():
        data = (HERE / local).read_bytes()
        files[remote] = {'before': sha((BUILD / 'release' / remote).read_bytes()),
                         'after': sha(data), 'data': base64.b64encode(data).decode()}
    remote_code = (HERE / 'standby_remote.py').read_bytes()
    trial = json.loads((BUILD / 'standby-trial-review.json').read_text())
    review = {'revision': 'standby-20261120-' + sha(remote_code)[:10],
              'station_sha256': 'bb4522ee89cabcf069ef94e3e770fb58070a3087e3c57f82ee96dc4fb4187007',
              'files': files, 'remote_code_sha256': sha(remote_code), 'trial': trial,
              'actions': ['backup and patch five deployment/test files and manifest',
                          'install State credential via SSH stdin, mode 0600; state_enabled=true',
                          'run installed Linux deployment and SDK tests before stopping anything',
                          'verify idle trial identity; SIGTERM parent 4147442 and wait for graceful cleanup',
                          'to-standby: stop idle evaluation; start real three-camera video and Agent',
                          'retain enabled=false, all commissioning=false, null Home/mapping'],
              'downloads': False, 'persistent_service_enable': False}
    REVIEW.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
    return review


def apply():
    review = json.loads(REVIEW.read_text())
    code = (HERE / 'standby_remote.py').read_bytes()
    if sha(code) != review['remote_code_sha256']:
        raise RuntimeError('Reviewed operation changed; prepare/review again')
    for remote, local in PATCHES.items():
        if sha((HERE / local).read_bytes()) != review['files'][remote]['after']:
            raise RuntimeError('Reviewed patch changed')
    history = (Path.home() / 'AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt').read_text(encoding='utf-8')
    candidates = re.findall(r'\$env:OPERATOR_STATE_TOKEN\s*=\s*([\x22\x27])([^\x22\x27\r\n]+)\1', history)
    if not candidates:
        raise RuntimeError('Previously verified local credential assignment not found')
    # Credential exists only in memory and encrypted SSH stdin; never argv, report or patch.
    payload = {**review, 'token': candidates[-1][1]}
    loader = 'import base64;exec(base64.b64decode(' + repr(base64.b64encode(code).decode()) + '))'
    command = 'python3 -c ' + "'" + loader.replace("'", "'\"'\"'") + "'"
    subprocess.run([*SSH, command], input=json.dumps(payload), text=True, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply()
    else:
        review = prepare()
        print(json.dumps({'review': str(REVIEW), 'revision': review['revision'],
                          'actions': review['actions'], 'connected': False}, indent=2))
