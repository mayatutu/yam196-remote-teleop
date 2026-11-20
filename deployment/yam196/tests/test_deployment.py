import contextlib
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace as NS, ModuleType

import numpy as np
import pytest

from yam_deploy import runtime, control, calibration, robot, video
from conftest import RELEASE


@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / 'station'
    (root / 'config').mkdir(parents=True)
    (root / 'runtime').mkdir()
    cfg = json.loads((RELEASE / 'defaults/station.json').read_text())
    (root / 'config/station.json').write_text(json.dumps(cfg))
    monkeypatch.setattr(runtime, 'ROOT', root)
    monkeypatch.setattr(control, 'transition_lock', contextlib.nullcontext)
    return cfg, root


def commission(station):
    cfg, root = station
    cfg['enabled'] = cfg['state_enabled'] = True
    cfg['commissioning'] = dict.fromkeys(runtime.COMMISSIONING, True)
    cfg['server_mapping'] = {f'{side}_{key}': ','.join([value]*6)
                            for side in ('left','right') for key,value in
                            [('joint_signs','1'),('joint_scales','1'),('joint_offsets','0'),
                             ('joint_rate_limit_rad_s','1'),('align_rate_limit_rad_s','1')]}
    for arm in cfg['arms'].values(): arm['home'] = [0.] * 7
    (root / 'config/station.json').write_text(json.dumps(cfg))
    token = root / 'config/state.token'
    token.write_text('test-only-local-fixture')
    token.chmod(0o600)
    return cfg


class FakeSystem:
    def __init__(self, cfg):
        self.states = {u: True for u in cfg['eval_units']}
        self.actions = []
        self.fail_start = None
        self.fail_stop = None
        self.residual = []
        self.trials = []
        self.idle = True
        self.session = None

    def active(self, unit): return self.states.get(unit, False)
    def unit_state(self, unit): return {'LoadState': 'loaded', 'ActiveState': 'active' if self.active(unit) else 'inactive'}
    def start(self, unit):
        self.actions.append(('start', unit))
        self.states[unit] = True
        if unit == self.fail_start: raise RuntimeError('injected start failure')
    def stop(self, unit):
        self.actions.append(('stop', unit))
        if unit == self.fail_stop: raise RuntimeError('injected stop timeout')
        self.states[unit] = False
    def independent_blockers(self): return self.trials
    def blockers(self, **kwargs): return self.residual
    def eval_idle(self, cfg): return self.idle
    def wait_ready(self, since): pass
    def ipc(self, cmd):
        self.actions.append(('ipc', cmd))
        return {'ok': True, 'teleop_enabled': cmd == 'enable', 'active_session_id': self.session,
                'home_running': False, 'arm_stack_running': False}


def test_disabled_rejects_before_stopping_evaluation(station):
    system = FakeSystem(station[0])
    with pytest.raises(RuntimeError, match='disabled'): control.to_teleop(system)
    assert system.actions == []
    assert runtime.state()['mode'] == 'installed'


def connect_state(station):
    cfg, root = station
    cfg['state_enabled'] = True
    (root / 'config/station.json').write_text(json.dumps(cfg))
    p = root / 'config/state.token'
    p.write_text('test-only-local-fixture')
    p.chmod(0o600)
    return cfg


def test_standby_without_motion_commissioning_and_restore(station):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    control.to_standby(system)
    assert runtime.state()['mode'] == 'standby'
    assert all(system.active(u) for u in control.UNITS)
    assert ('ipc', 'enable') not in system.actions
    assert runtime.config()['enabled'] is False
    assert not any(runtime.config()['commissioning'].values())
    assert runtime.config()['server_mapping'] is None
    assert all(a['home'] is None for a in runtime.config()['arms'].values())
    control.to_eval(system)
    assert runtime.state()['mode'] == 'eval'
    assert all(system.active(u) for u in cfg['eval_units'])


@pytest.mark.parametrize('reason', ['token', 'state_disabled', 'trial', 'busy'])
def test_standby_preconditions_do_not_stop_evaluation(station, reason):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    if reason == 'token': (station[1] / 'config/state.token').unlink()
    if reason == 'state_disabled':
        cfg['state_enabled'] = False
        (station[1] / 'config/station.json').write_text(json.dumps(cfg))
    if reason == 'trial': system.trials = [4001]
    if reason == 'busy': system.idle = False
    with pytest.raises(RuntimeError): control.to_standby(system)
    assert system.actions == []
    assert runtime.state()['mode'] == 'installed'


def test_standby_fault_retry_preserves_original_evaluation_snapshot(station):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    system.states.clear()
    system.idle = False  # Evaluation HTTP is no longer running; must not require it.
    runtime.write_state({'mode': 'fault', 'previous_eval': cfg['eval_units']})
    control.retry_standby(system)
    assert runtime.state()['mode'] == 'standby'
    assert runtime.state()['previous_eval'] == cfg['eval_units']
    assert ('ipc', 'enable') not in system.actions
    control.to_eval(system)
    assert all(system.active(u) for u in cfg['eval_units'])


@pytest.mark.parametrize('reason', ['not_fault', 'evaluation', 'foreign', 'managed'])
def test_standby_fault_retry_requires_released_devices(station, reason):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    if reason != 'evaluation': system.states.clear()
    if reason == 'foreign': system.residual = [4002]
    if reason == 'managed': system.states[control.AGENT] = True
    runtime.write_state({'mode': 'installed' if reason == 'not_fault' else 'fault',
                         'previous_eval': cfg['eval_units']})
    with pytest.raises(RuntimeError): control.retry_standby(system)
    assert system.actions == []


@pytest.mark.parametrize('mode', ['standby-starting', 'standby'])
def test_standby_allows_only_agent_video_even_if_commissioned(station, monkeypatch, mode):
    cfg = commission(station)
    system = FakeSystem(cfg)
    system.states.clear()
    monkeypatch.setattr(control, 'System', lambda: system)
    runtime.write_state({'mode': mode})
    for kind in ('agent', 'video'):
        runtime.require_running(kind)
    for kind in ('enable', 'server', 'handoff', 'bridge', 'home', 'robot', 'runtime'):
        with pytest.raises(RuntimeError, match='blocked in standby'):
            runtime.require_running(kind)
    from remote_teleop.arena_agent import ArenaAgent
    with pytest.raises(RuntimeError, match='blocked in standby'):
        ArenaAgent.teleop_enable(NS())
    with pytest.raises(RuntimeError, match='blocked in standby'):
        robot.open_arm('can_left')
    from remote_teleop.home_move import run_home_move
    with pytest.raises(RuntimeError, match='blocked in standby'):
        run_home_move(left_config_path='nonexistent')


@pytest.mark.parametrize('reason', ['evaluation', 'foreign'])
def test_standby_entrypoints_keep_device_exclusivity(station, monkeypatch, reason):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    if reason == 'foreign':
        system.states.clear()
        system.residual = [4002]
    monkeypatch.setattr(control, 'System', lambda: system)
    runtime.write_state({'mode': 'standby'})
    for kind in ('agent', 'video'):
        with pytest.raises(RuntimeError): runtime.require_running(kind)


@pytest.mark.parametrize('bad_status', [
    {'teleop_enabled': True}, {'active_session_id': 'unexpected'},
    {'home_running': True}, {'arm_stack_running': True},
])
def test_standby_rejects_motion_and_cleans_up(station, bad_status):
    cfg = connect_state(station)
    system = FakeSystem(cfg)
    original = system.ipc
    system.ipc = lambda cmd: {**original(cmd), **bad_status}
    with pytest.raises(RuntimeError, match='motion-disabled standby'):
        control.to_standby(system)
    assert runtime.state()['mode'] == 'fault'
    assert not any(system.active(u) for u in control.UNITS)
    assert not any(system.active(u) for u in cfg['eval_units'])


@pytest.mark.parametrize('missing', runtime.COMMISSIONING)
def test_each_commissioning_gate(station, missing):
    cfg = commission(station)
    cfg['commissioning'][missing] = False
    (station[1] / 'config/station.json').write_text(json.dumps(cfg))
    with pytest.raises(RuntimeError, match=missing): runtime.require_commissioned()


def test_missing_token(station):
    commission(station)
    (station[1] / 'config/state.token').unlink()
    with pytest.raises(RuntimeError, match='authentication'): runtime.require_commissioned()


@pytest.mark.parametrize('reason', ['busy', 'trial', 'residual', 'stop_timeout', 'start_failure'])
def test_switch_failure_never_restarts_evaluation(station, reason):
    cfg = commission(station)
    system = FakeSystem(cfg)
    if reason == 'busy': system.idle = False
    if reason == 'trial': system.trials = [4001]
    if reason == 'residual': system.residual = [4002]
    if reason == 'stop_timeout': system.fail_stop = cfg['eval_units'][0]
    if reason == 'start_failure': system.fail_start = control.AGENT
    with pytest.raises(RuntimeError): control.to_teleop(system)
    assert not any(action == 'start' and unit in cfg['eval_units'] for action, unit in system.actions)
    if reason in ('busy', 'trial'):
        assert not system.actions
    else:
        assert runtime.state()['mode'] == 'fault'


def test_switch_and_explicit_restore_only_previous_units(station):
    cfg = commission(station)
    system = FakeSystem(cfg)
    system.states['yam2-policy.service'] = False
    control.to_teleop(system)
    assert runtime.state()['mode'] == 'teleop'
    assert not any(system.active(u) for u in cfg['eval_units'])
    control.to_eval(system)
    assert runtime.state()['mode'] == 'eval'
    assert not system.active('yam2-policy.service')
    assert system.active('yam2-gui.service')
    assert not any(system.active(u) for u in control.UNITS)


def test_active_session_blocks_return_to_eval(station):
    cfg = commission(station)
    system = FakeSystem(cfg)
    control.to_teleop(system)
    system.session = 'active-session'
    before = list(system.actions)
    with pytest.raises(RuntimeError, match='active session'): control.to_eval(system)
    assert system.actions[len(before):] == [('ipc', 'status')]


def test_partial_eval_restore_is_stopped_on_failure(station):
    cfg = commission(station)
    system = FakeSystem(cfg)
    control.to_teleop(system)
    system.fail_start = 'yam2-web.service'
    with pytest.raises(RuntimeError): control.to_eval(system)
    assert runtime.state()['mode'] == 'fault'
    assert not any(system.active(u) for u in cfg['eval_units'])


def test_fault_cannot_start_again(station):
    cfg = commission(station)
    runtime.write_state({'mode': 'fault', 'previous_eval': []})
    system = FakeSystem(cfg)
    with pytest.raises(RuntimeError, match='Fault'): control.to_teleop(system)
    assert not system.actions


def test_video_configs_and_profiles(station):
    commands = {role: video.command(role) for role in ('head', 'left', 'right')}
    assert len({c[c.index('--session')+1] for c in commands.values()}) == 3
    assert all('realsense:' + station[0]['cameras'][r]['serial'] in c for r, c in commands.items())
    assert '180000' in video.command('left', selected='reduced')
    with pytest.raises(RuntimeError): video.command('right', selected='head_only')


def test_stale_or_wrong_video_health_rejected(station):
    assert not video.health_ready(0)
    for role, cam in station[0]['cameras'].items():
        (station[1] / f'runtime/video-{role}.json').write_text(json.dumps(
            {'session': cam['session'], 'encoded_total': 5, 'encoded_fps': 30}))
    assert video.health_ready(time.time()-1)
    assert not video.health_ready(time.time()+1)
    (station[1] / 'runtime/video-left.json').write_text('{"session":"wrong","encoded_total":5,"encoded_fps":30}')
    assert not video.health_ready(0)


class FakeArm:
    def __init__(self, limits, position, offset=0., direction=1):
        self._gripper_limits = np.asarray(limits).copy()
        self._command_lock = contextlib.nullcontext()
        self._state_lock = contextlib.nullcontext()
        self._gripper_index = 6
        self._commands = NS(kp=np.zeros(7), kd=np.zeros(7), torques=np.zeros(7), pos=np.zeros(7))
        self._gripper_force_limiter = NS(_gripper_adjusted_qpos=None, _is_clogged=False)
        self.states = [NS(id=i+1, pos=0. if i<6 else position) for i in range(7)]
        class Chain:
            def __len__(self): return 7
        self.motor_chain = Chain()
        self.motor_chain.running = True
        self.motor_chain.motor_offset = np.array([0.]*6 + [offset])
        self.motor_chain.motor_direction = np.array([1.]*6 + [direction])
        self.motor_chain.read_states = lambda: self.states
        self.held = False
        self.closed = False
    def get_joint_pos(self):
        low, high = self._gripper_limits
        return np.array([0.]*6 + [(self.states[6].pos-low)/(high-low)])
    def _motor_state_to_joint_state(self, states): return self.get_joint_pos()
    def command_joint_pos(self, target): self.held = True
    def close(self): self.closed = True


@pytest.mark.parametrize('position,offset,expected_shift', [(3.,0.,0.),(3.+2*np.pi,0.,2*np.pi),(2.5,.5,-.5)])
def test_calibration_reuses_physical_endpoints(monkeypatch, position, offset, expected_shift):
    limits = [6.4242389562829025, 1.2201495384145868]
    arm = FakeArm(limits, position, offset)
    factory = ModuleType('i2rt.robots.get_robot')
    def get_robot(**kwargs):
        assert kwargs['zero_gravity_mode'] is True
        assert kwargs['gripper_limits_override'] == limits
        return arm
    factory.get_yam_robot = get_robot
    utils = ModuleType('i2rt.robots.utils')
    utils.GripperType = NS(LINEAR_4310='linear_4310')
    utils.JointMapper = lambda **kwargs: NS(**kwargs)
    monkeypatch.setitem(sys.modules, 'i2rt.robots.get_robot', factory)
    monkeypatch.setitem(sys.modules, 'i2rt.robots.utils', utils)
    assert calibration._open_arm('can_left', limits, calibration_offset=0.) is arm
    np.testing.assert_allclose(arm._gripper_limits, np.array(limits)+expected_shift)
    assert arm.held


def test_bad_gripper_feedback_never_holds(monkeypatch):
    arm = FakeArm([6.4,1.2], float('nan'))
    factory = ModuleType('i2rt.robots.get_robot'); factory.get_yam_robot = lambda **kw: arm
    utils = ModuleType('i2rt.robots.utils'); utils.GripperType = NS(LINEAR_4310='linear_4310')
    monkeypatch.setitem(sys.modules, 'i2rt.robots.get_robot', factory)
    monkeypatch.setitem(sys.modules, 'i2rt.robots.utils', utils)
    with pytest.raises(ValueError): calibration._open_arm('can_left', [6.4,1.2], calibration_offset=0.)
    assert arm.closed and not arm.held


def test_server_right_init_failure_closes_left(tmp_path, monkeypatch):
    import yam_remote_dual_server as server
    args = server.build_parser(server.build_defaults_from_layers({})).parse_args(['--no-allow-single-arm', '--dry-run', '--debug-run-dir', str(tmp_path)])
    arm = server.MockRobot(server.DEFAULT_JOINT_LIMITS)
    closed = []
    arm.close = lambda: closed.append(True)
    def create(channel, args):
        if channel == 'can_right': raise RuntimeError('right failure')
        return arm
    monkeypatch.setattr(server, 'create_robot', create)
    with pytest.raises(RuntimeError, match='right failure'): server.setup_arms(args)
    assert closed == [True]


def test_close_tries_both_and_reports_failure():
    called = []
    def bad(): called.append('bad'); raise RuntimeError('thread alive')
    with pytest.raises(RuntimeError, match='thread alive'):
        robot.close_many([NS(close=bad), NS(close=lambda: called.append('good'))])
    assert called == ['bad', 'good']


def test_bridge_separates_sessions():
    from arm_relay_server_bridge import extract_pmrc_from_relay
    from relay_bridge_proto import encode_relay_header, RELAY_TYPE_RTP
    data = encode_relay_header(RELAY_TYPE_RTP, 'other') + b'PMRC-payload'
    assert extract_pmrc_from_relay(data, 'yam_arm_SZ_YAM02') is None


def test_real_entrypoints_block_before_constructing(station, monkeypatch):
    with pytest.raises(RuntimeError, match='disabled'): robot.open_arm('can_left')
    from remote_teleop.home_move import run_home_move
    with pytest.raises(RuntimeError, match='disabled'): run_home_move(left_config_path='nonexistent')
    from remote_teleop.can_handoff import run_can_probe_subprocess
    with pytest.raises(RuntimeError, match='disabled'): run_can_probe_subprocess()


def test_can_handoff_allows_owned_video_but_blocks_every_arm_owner(station, monkeypatch):
    commission(station)
    runtime.write_state({'mode': 'teleop'})
    system = control.System()
    monkeypatch.setattr(system, 'active', lambda unit: False)
    owned = str(runtime.ROOT) + '/current/'
    processes = [(10, owned + 'low-latency-teleop/sender --mode relay')]
    monkeypatch.setattr(system, 'processes', lambda: processes)
    monkeypatch.setattr(control, 'System', lambda: system)
    from remote_teleop.can_handoff import run_can_probe_subprocess
    assert run_can_probe_subprocess().ok
    assert system.blockers(include_managed=True) == [10]  # Full release still checks cameras.
    for command in ('piper-mate/Python_SDK/yam_remote_dual_server.py',
                    'remote_teleop.home_move', 'piper-mate/Python_SDK/arm_relay_server_bridge.py'):
        processes[:] = [(10, owned + 'low-latency-teleop/sender'), (11, owned + command)]
        result = run_can_probe_subprocess()
        assert not result.ok and result.residual_pids == (11,)
    processes[:] = [(12, '/outside/launch_yaml_eval_molmoact')]
    with pytest.raises(RuntimeError, match='unmanaged'):
        run_can_probe_subprocess()


def test_armstack_verified_stop_imports_allstop_and_stops_both_children(monkeypatch, tmp_path):
    from remote_teleop.arm_stack import ArmStack, ArmStackRuntime
    from remote_teleop.phase import RemoteTeleopConfig
    from remote_teleop.process_manager import StopResult
    from remote_teleop import all_stop
    from remote_dual_protocol import decode_command
    assert decode_command(all_stop.encode_all_stop_packet()).all_stop
    events = []
    monkeypatch.setattr(all_stop, 'send_all_stop', lambda **kw: events.append('all_stop'))
    stack = ArmStack(ArmStackRuntime(python=sys.executable, client_root=tmp_path, config=RemoteTeleopConfig()))
    def stop(name, **kw):
        events.append(name)
        return StopResult(name=name, verified=True, reason='exited')
    stack.pm = NS(get=lambda name: NS(is_alive=lambda: True), stop_verified=stop)
    assert stack.stop_verified().verified
    assert events == ['all_stop', 'arm_bridge', 'yam_server']


def test_udp_loopback_dry_server(tmp_path):
    from remote_dual_protocol import ArmBlockV3, DualCommandV3, encode_command, decode_status
    reserve = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1];reserve.close()
    env = {k:v for k,v in os.environ.items() if not k.startswith('YAM_REMOTE_')}
    env['PYTHONPATH'] = os.pathsep.join([str(RELEASE/'python'),str(RELEASE/'piper-mate/Python_SDK')])
    cmd = [sys.executable, str(RELEASE/'piper-mate/Python_SDK/yam_remote_dual_server.py'),
           '--dry-run','--no-allow-single-arm','--bind-ip','127.0.0.1','--port',str(port),
           '--debug-run-dir',str(tmp_path/'debug')]
    with (tmp_path/'server.log').open('w') as log:
        proc = subprocess.Popen(cmd,env=env,stdout=log,stderr=log)
        sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.settimeout(.2)
        try:
            deadline=time.monotonic()+8
            packet=encode_command(DualCommandV3(time.time_ns(),ArmBlockV3(1,(0,)*6),ArmBlockV3(1,(0,)*6),all_stop=True))
            assert len(packet)==88
            while True:
                assert proc.poll() is None, (tmp_path/'server.log').read_text()
                sock.sendto(packet,('127.0.0.1',port))
                try:
                    reply=decode_status(sock.recvfrom(65535)[0]);break
                except (socket.timeout, ConnectionResetError):
                    if time.monotonic()>deadline: raise
            assert set(reply.arms)=={'left','right'}
            assert all(arm.safe_state for arm in reply.arms.values())
        finally:
            sock.close();proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()


@pytest.mark.parametrize('failure',['right','close'])
def test_home_cleanup_failure_is_not_success(station,monkeypatch,failure):
    import yaml
    import remote_teleop.home_move as home
    monkeypatch.setattr(runtime,'require_running',lambda kind:station[0])
    configs=[]
    for side in ('left','right'):
        p=station[1]/f'{side}.yaml';p.write_text(yaml.safe_dump({'agent':{'start_joints':[0.]*7},'robot':{}}));configs.append(p)
    opened=[];closed=[]
    def build(cfg):
        if failure=='right' and opened:raise RuntimeError('right failed')
        def close():
            closed.append(True)
            if failure=='close':raise RuntimeError('close failed')
        arm=NS(close=close);opened.append(arm);return arm
    monkeypatch.setattr(home,'_build_robot_from_cfg',build)
    monkeypatch.setattr(home,'RobotEnv',lambda *a,**kw:NS(get_obs=lambda:{'joint_positions':np.zeros(14)}))
    assert home.run_home_move(left_config_path=configs[0],right_config_path=configs[1])==1
    assert len(closed)==len(opened)


def test_close_thread_timeout_is_reported_after_socket_cleanup(monkeypatch):
    events=[]
    chain=NS(running=True)
    chain.motor_interface=NS(close=lambda:events.append('socket'))
    arm=NS(motor_chain=chain, close=lambda:chain.motor_interface.close())
    thread=NS(_target=NS(__self__=chain),name='can-worker',join=lambda timeout:events.append('join'),is_alive=lambda:True)
    monkeypatch.setattr(calibration.threading,'enumerate',lambda:[thread])
    with pytest.raises(RuntimeError,match='did not stop'):calibration._close_arm(arm)
    assert events==['join','socket'] and not chain.running


@pytest.mark.skipif(os.name!='posix',reason='fcntl only exists on Linux')
def test_real_concurrent_switch_lock(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,'ROOT',tmp_path)
    with control.transition_lock():
        with pytest.raises(RuntimeError,match='Another transition'):
            with control.transition_lock():pass
