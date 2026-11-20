"""Test explicit motion scope, immutable reviewed parameters, and standby recovery."""
import contextlib
import json
from pathlib import Path

import pytest
import yaml

from yam_deploy import runtime, control, configure, mirror_test
from test_deployment import station, connect_state, FakeSystem


def setup(station, monkeypatch):
    cfg = connect_state(station)
    configure.render(station[1])
    system = FakeSystem(cfg)
    system.states = dict.fromkeys(control.UNITS, True)
    system.enabled = False
    base_ipc = system.ipc
    def ipc(cmd):
        result = base_ipc(cmd)
        if cmd in ('enable', 'disable'):
            system.enabled = cmd == 'enable'
        result.update(teleop_enabled=system.enabled, state_connected=True, state_synced=True)
        return result
    system.ipc = ipc
    monkeypatch.setattr(mirror_test, 'transition_lock', contextlib.nullcontext)
    monkeypatch.setattr(control, 'System', lambda: system)
    runtime.write_state({'mode': 'standby', 'previous_eval': cfg['eval_units']})
    return system


def test_test_scope_preserves_station_parameters_and_returns_standby(station, monkeypatch):
    system = setup(station, monkeypatch)
    path = station[1] / 'config/station.json'
    original = path.read_bytes()
    mirror_test.start(system)
    assert runtime.state()['mode'] == 'mirror-test'
    assert path.read_bytes() == original
    normal = yaml.safe_load(mirror_test.NORMAL.read_text(encoding='utf-8'))
    server = yaml.safe_load((station[1] / 'config/mirror-test/server.yaml').read_text())
    for k in normal.keys() - {'bind_ip', 'port', 'allow_single_arm'}:
        assert server[k] == normal[k], k
    for k in ('agent', 'video', 'enable', 'handoff', 'server', 'bridge', 'robot'):
        runtime.require_running(k)
    for k in ('home', 'runtime', 'unknown'):
        with pytest.raises(RuntimeError, match='Home is prohibited'):
            runtime.require_running(k)
    with pytest.raises(RuntimeError, match='disabled'):
        runtime.require_commissioned()
    mirror_test.stop(system)
    assert runtime.state()['mode'] == 'standby'
    assert runtime.state()['previous_eval'] == station[0]['eval_units']
    assert path.read_bytes() == original
    assert {u for op, u in system.actions if op in ('start', 'stop')} == {control.AGENT}
    with pytest.raises(RuntimeError, match='blocked in standby'):
        runtime.require_running('robot')


@pytest.mark.parametrize('reason', ['session', 'enabled', 'eval', 'foreign', 'video'])
def test_refuses_busy_start_before_agent_stop(station, monkeypatch, reason):
    system = setup(station, monkeypatch)
    if reason == 'session': system.session = 'busy'
    if reason == 'enabled': system.enabled = True
    if reason == 'eval': system.states[station[0]['eval_units'][0]] = True
    if reason == 'foreign': system.residual = [123]
    if reason == 'video': system.states[control.VIDEOS[0]] = False
    with pytest.raises(RuntimeError): mirror_test.start(system)
    assert not any(op in ('start', 'stop', 'enable') for op, _ in system.actions)
    assert runtime.state()['mode'] == 'standby'


@pytest.mark.parametrize('target', ['station', 'agent', 'server', 'purpose'])
def test_changed_approval_blocks_hardware(station, monkeypatch, target):
    system = setup(station, monkeypatch)
    mirror_test.start(system)
    if target == 'purpose':
        value = runtime.state()
        value['purpose'] = 'invalid'
        runtime.write_state(value)
    else:
        p = station[1] / ('config/station.json' if target == 'station' else f'config/mirror-test/{target}.yaml')
        p.write_text(p.read_text() + '\n')
    with pytest.raises(RuntimeError, match='changed'):
        runtime.require_running('robot')


def test_busy_stop_leaves_owner_running(station, monkeypatch):
    system = setup(station, monkeypatch)
    mirror_test.start(system)
    system.session = 'live'
    system.actions.clear()
    with pytest.raises(RuntimeError, match='verify arm release'):
        mirror_test.stop(system)
    assert system.active(control.AGENT)
    assert runtime.state()['mode'] == 'mirror-test'
    assert system.actions == [('ipc', 'status')]


@pytest.mark.parametrize('failure', ['start', 'stop'])
def test_restart_failure_latches_fault_without_restoring_evaluation(station, monkeypatch, failure):
    system = setup(station, monkeypatch)
    if failure == 'start': system.fail_start = control.AGENT
    else: system.fail_stop = control.AGENT
    with pytest.raises(RuntimeError): mirror_test.start(system)
    assert runtime.state()['mode'] == 'fault'
    assert runtime.state()['previous_eval'] == station[0]['eval_units']
    assert not any(system.active(u) for u in station[0]['eval_units'])
