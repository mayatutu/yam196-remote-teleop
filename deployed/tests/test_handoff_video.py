import pytest
from yam_deploy import runtime,control
from test_deployment import station,commission

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
