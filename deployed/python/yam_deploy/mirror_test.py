"""Explicit commissioning test; normal station/Home acceptance is never changed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import yaml

from . import runtime
from .control import AGENT, UNITS, System, transition_lock

PURPOSE = 'one-Mate-two-real-YAM-test-no-Home'
NORMAL = Path(__file__).with_name('mirror_normal_server.yaml')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_profile():
    cfg = runtime.require_state_connection()
    server = yaml.safe_load(NORMAL.read_text(encoding='utf-8'))
    # These are endpoint/ownership changes only. All normal motion parameters survive.
    server.update(bind_ip='127.0.0.1', port=5012, allow_single_arm=False,
                  debug_run_dir=str(runtime.ROOT / 'logs/mirror-test'))
    agent = yaml.safe_load((runtime.ROOT / 'config/agent.yaml').read_text(encoding='utf-8'))
    agent['remote_teleop']['robot']['yam_server_config'] = str(runtime.ROOT / 'config/mirror-test/server.yaml')
    folder = runtime.ROOT / 'config/mirror-test'
    folder.mkdir(exist_ok=True)
    for name, value in (('server', server), ('agent', agent)):
        (folder / f'{name}.yaml').write_text(yaml.safe_dump(value, sort_keys=False), encoding='utf-8')
    return {'purpose': PURPOSE, 'station_sha256': digest(runtime.ROOT / 'config/station.json'),
            'normal_sha256': digest(NORMAL),
            'profile_sha256': {n: digest(folder / n) for n in ('agent.yaml', 'server.yaml')}}


def require_profile(current):
    cfg = runtime.require_state_connection()
    if (current.get('purpose') != PURPOSE
            or current.get('station_sha256') != digest(runtime.ROOT / 'config/station.json')
            or current.get('normal_sha256') != digest(NORMAL)):
        raise RuntimeError('One-Mate test approval/configuration changed')
    expected = current.get('profile_sha256', {})
    folder = runtime.ROOT / 'config/mirror-test'
    if set(expected) != {'agent.yaml', 'server.yaml'} or any(digest(folder / n) != h for n, h in expected.items()):
        raise RuntimeError('One-Mate test profile changed')
    return cfg


def idle(status):
    return (status.get('ok') is True and status.get('teleop_enabled') is False
            and not any(status.get(k) for k in ('active_session_id', 'session_id', 'teleop_session_id',
                                               'home_running', 'arm_stack_running')))


def wait_agent(system, *, enabled):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not all(system.active(u) for u in UNITS):
            raise RuntimeError('Agent or existing video stopped')
        try:
            reply = system.ipc('status')
            if (reply.get('ok') and reply.get('state_connected') and reply.get('state_synced')
                    and reply.get('teleop_enabled') is enabled):
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(.25)
    raise RuntimeError('Test Agent State synchronization timed out')


def start(system=None):
    system = system or System()
    with transition_lock():
        old = runtime.state()
        cfg = runtime.require_state_connection()
        if old.get('mode') != 'standby' or not idle(system.ipc('status')):
            raise RuntimeError('Test requires idle, disabled standby')
        if (not all(system.active(u) for u in UNITS)
                or any(system.active(u) for u in cfg['eval_units'])
                or system.blockers(include_managed=False)):
            raise RuntimeError('Test requires healthy standby and exclusive device ownership')
        profile = prepare_profile()
        # Only Agent restarts; keep all three running camera services and original eval snapshot.
        try:
            system.stop(AGENT)
            runtime.write_state({**old, **profile, 'mode': 'mirror-test-starting'})
            system.start(AGENT)
            wait_agent(system, enabled=False)
            reply = system.ipc('enable')
            if not reply.get('ok') or reply.get('teleop_enabled') is not True:
                raise RuntimeError('Test capability enable failed')
            wait_agent(system, enabled=True)
            runtime.write_state({**runtime.state(), 'mode': 'mirror-test'})
        except BaseException as exc:
            cleanup = []
            try:
                system.stop(AGENT)
            except Exception as stop_exc:
                cleanup.append(str(stop_exc))
            runtime.write_state({**old, 'mode': 'fault', 'fault': f'mirror-test: {exc}', 'cleanup_errors': cleanup})
            raise


def stop(system=None):
    system = system or System()
    with transition_lock():
        old = runtime.state()
        if old.get('mode') not in ('mirror-test', 'mirror-test-starting'):
            raise RuntimeError('No active one-Mate test')
        status = system.ipc('status')
        # UI abort must release the real ArmStack before changing the process owner.
        if any(status.get(k) for k in ('active_session_id', 'session_id', 'teleop_session_id',
                                      'home_running', 'arm_stack_running')) or not status.get('ok'):
            raise RuntimeError('Stop/abort the Operator session and verify arm release first')
        if not idle(system.ipc('disable')):
            raise RuntimeError('Test disable did not confirm idle')
        standby = {k: v for k, v in old.items() if k not in
                   ('purpose', 'station_sha256', 'normal_sha256', 'profile_sha256')}
        try:
            system.stop(AGENT)
            runtime.write_state({**standby, 'mode': 'standby'})
            system.start(AGENT)
            wait_agent(system, enabled=False)
        except BaseException as exc:
            cleanup = []
            try:
                system.stop(AGENT)
            except Exception as stop_exc:
                cleanup.append(str(stop_exc))
            runtime.write_state({**standby, 'mode': 'fault', 'fault': f'test stop: {exc}', 'cleanup_errors': cleanup})
            raise
