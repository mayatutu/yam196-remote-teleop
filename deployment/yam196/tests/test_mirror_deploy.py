"""Exercise the deployment transaction with temporary files and no SSH/devices."""
import base64
import contextlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import mirror_remote
from yam_deploy import control


@pytest.mark.parametrize('fail_tests', [False, True])
def test_deploy_backup_tests_then_enable_or_exact_file_rollback(tmp_path, monkeypatch, fail_tests):
    release = tmp_path / 'current'
    release.mkdir()
    (tmp_path / 'backups').mkdir()
    originals = {}
    for n in (*mirror_remote.NAMES, 'manifest.json'):
        p = release / n
        p.parent.mkdir(parents=True, exist_ok=True)
        data = b'{"files":{}}' if n == 'manifest.json' else b'original'
        if n == 'python/yam_deploy/mirror_test.py':
            data = None
        if data is not None: p.write_bytes(data)
        originals[n] = data
    snapshot = {'files': {n: mirror_remote.digest(release / n) for n in originals},
                'station_sha256': 'fixture', 'mode': {'mode': 'standby'}, 'blockers': [],
                'agent': {'ok': True, 'teleop_enabled': False},
                'units': dict.fromkeys(control.UNITS, True)}
    monkeypatch.setattr(mirror_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(mirror_remote, 'inspect', lambda: snapshot)
    monkeypatch.setattr(control, 'transition_lock', contextlib.nullcontext)
    actions = []
    def run(cmd, **kwargs):
        actions.append(cmd)
        if 'pytest' in cmd and fail_tests:
            raise subprocess.CalledProcessError(1, ['pytest'])
    monkeypatch.setattr(mirror_remote.subprocess, 'run', run)
    payload = {'snapshot': snapshot, 'files': {n: base64.b64encode(b'new fixture').decode()
                                             for n in mirror_remote.NAMES}}
    if fail_tests:
        with pytest.raises(subprocess.CalledProcessError): mirror_remote.apply(payload)
        assert len(actions) == 1 and 'pytest' in actions[0]
        for n, data in originals.items():
            assert ((release / n).read_bytes() if (release / n).exists() else None) == data
    else:
        mirror_remote.apply(payload)
        assert len(actions) == 2 and 'pytest' in actions[0]
        assert actions[1][-1] == 'mirror-test-start'
        for n in mirror_remote.NAMES:
            assert (release / n).read_bytes() == b'new fixture'
    backups = list((tmp_path / 'backups').iterdir())
    assert len(backups) == 1
    for n, data in originals.items():
        if data is not None: assert (backups[0] / n).read_bytes() == data
