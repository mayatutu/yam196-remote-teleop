"""Offline review by default; --inspect reads SSH; --apply enables real test capability."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

HERE = Path(__file__).resolve().parent
BUILD = HERE / '.build/windows-mirror-test'
REVIEW = BUILD / 'review.json'
SNAPSHOT = BUILD / 'standby-snapshot.json'
FILES = {'launch.py': 'launch.py', 'python/yam_deploy/runtime.py': 'yam_deploy/runtime.py',
         'python/yam_deploy/mirror_test.py': 'yam_deploy/mirror_test.py',
         'python/yam_deploy/mirror_normal_server.yaml': 'yam_deploy/mirror_normal_server.yaml',
         'tests/test_mirror_runtime.py': 'tests/test_mirror_runtime.py'}
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes', 'yam196']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def remote(operation, *, capture=False):
    code = (HERE / 'mirror_remote.py').read_bytes()
    loader = 'import base64;exec(compile(base64.b64decode(' + repr(base64.b64encode(code).decode()) + "),'<mirror-deploy>','exec'))"
    result = subprocess.run([*SSH, 'python3 -c ' + shlex.quote(loader)],
                            input=json.dumps(operation), text=True, capture_output=capture)
    if result.returncode:
        raise RuntimeError('SSH operation failed: ' + (result.stderr[-2500:] if capture else str(result.returncode)))
    return result


def prepare():
    snapshot = json.loads(SNAPSHOT.read_text())
    files = {n: base64.b64encode((HERE / p).read_bytes()).decode() for n, p in FILES.items()}
    code = (HERE / 'mirror_remote.py').read_bytes()
    review = {'operation': 'apply', 'snapshot': snapshot, 'files': files,
              'remote_code_sha256': sha(code),
              'local_hashes': {p: sha((HERE / p).read_bytes()) for p in (
                  *FILES.values(), 'mirror_mate_test.py', 'open_operator_mirror_test.py')},
              'actions': ['backup and patch explicit test guards; run installed hardware-free tests',
                          'restart only Agent into dedicated test mode; keep cameras and evaluation snapshot',
                          'enable test capability; no UART/CAN initialization until Operator Teleop start',
                          'normal speed parameters retained; Home prohibited; stop uses abort']}
    REVIEW.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'review': str(REVIEW), 'connected': False, 'actions': review['actions']}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--inspect', action='store_true')
    g.add_argument('--apply', action='store_true')
    args = p.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    if args.inspect:
        result = remote({'operation': 'inspect'}, capture=True)
        snapshot = json.loads(result.stdout)
        SNAPSHOT.write_text(json.dumps(snapshot, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(snapshot, indent=2))
    elif args.apply:
        review = json.loads(REVIEW.read_text())
        if sha((HERE / 'mirror_remote.py').read_bytes()) != review['remote_code_sha256']:
            raise RuntimeError('Reviewed operation changed')
        for path, expected in review['local_hashes'].items():
            if sha((HERE / path).read_bytes()) != expected:
                raise RuntimeError('Reviewed local source changed: ' + path)
        remote(review)
    else:
        prepare()


if __name__ == '__main__':
    main()
