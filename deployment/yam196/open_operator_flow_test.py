"""Open the existing Operator with dry hardware and real YAM video; no Mate required.

Commands/Agent/State use an isolated loopback session. Public Relay is video-only.
The explicit --flow-test flag prevents accidentally treating this as live control.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = Path('E:/working/YAM/YAM/arena-client/yam_client')
BUNDLE = Path('E:/working/YAM/YAM/arena-client/dist/YAM Operator-video-health')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flow-test', action='store_true', required=True)
    parser.add_argument('--verify', action='store_true', help='Exercise one start/stop cycle, then leave UI open')
    args = parser.parse_args()
    work = HERE / '.build' / ('operator-flow-' + uuid.uuid4().hex[:8])
    work.mkdir()
    os.chdir(work)
    sys.path.insert(0, str(SOURCE))
    os.environ.pop('QT_QPA_PLATFORM', None)
    import uvicorn
    import yaml
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel
    from operator_state.config import RobotEntry, StateServerConfig
    from operator_state.main import create_app
    from remote_teleop.arena_agent import ArenaAgent
    from remote_teleop.phase import RemoteTeleopConfig
    from yam_operator.core.cam_third_embed import CamThirdManager
    from yam_operator.main import build_app
    from yam_operator.process.process_supervisor import ProcessSupervisor
    from yam_operator.ui.operator_mode import OperatorMode

    rid = 'FLOW_TEST'
    token = secrets.token_urlsafe(24)
    os.environ['YAM_FLOW_TEST_TOKEN'] = token
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    arm_session = 'flow_' + uuid.uuid4().hex[:16]
    server = uvicorn.Server(uvicorn.Config(create_app(StateServerConfig(
        host='127.0.0.1', port=port, token=token, sqlite_path=work / 'state.db',
        robots=[RobotEntry(robot_id=rid, arm_session=arm_session)])),
        log_level='error', access_log=False))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError('Local flow State failed to start')
        time.sleep(.02)
    agent_config = work / 'agent.yaml'
    agent_config.write_text(yaml.safe_dump({'robot_id': rid, 'remote_teleop': {
        'state': {'enabled': True, 'server': url, 'token': '${YAM_FLOW_TEST_TOKEN}'}}}), encoding='utf-8')
    agent = ArenaAgent(config_path=str(agent_config), python=sys.executable,
                       client_root=work, relay_host='127.0.0.1', relay_port=port,
                       agent_session=arm_session, robot_id=rid,
                       teleop_config=RemoteTeleopConfig(), dry_run=True)
    agent._state_bridge.start()
    cfg = yaml.safe_load((BUNDLE / 'config/profiles.yaml').read_text(encoding='utf-8'))
    robot = next(r for r in cfg['robots'] if r['robot_id'] == 'SZ_YAM02')
    robot['robot_id'] = rid
    robot['sessions'].update(agent=arm_session, teleop=arm_session, arm=arm_session)
    # Keep the three original public camera sessions. Arm/control sessions are isolated.
    cfg['robots'] = [robot]
    cfg['state'].update(server=url, websocket=url.replace('http', 'ws', 1),
                        token='${YAM_FLOW_TEST_TOKEN}', fallback_relay_control=False,
                        manual_teleop_enabled=True)
    # Keep a fallback tab while capability synchronization briefly hides Teleop.
    cfg['ui'].update(visible_modes=['start', 'teleop'], auto_close_review=False)
    for key, value in cfg['paths'].items():
        cfg['paths'][key] = str((BUNDLE / value).resolve())
    profile = work / 'profiles.flow-test.yaml'
    profile.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding='utf-8')
    # Reuse existing dry hardware switch, but use a real receiver supervisor for video.
    def video_factory(_supervisor, config, **_kwargs):
        return CamThirdManager(ProcessSupervisor(), config, dry_run=False)
    with patch('yam_operator.core.cam_third_embed.CamThirdManager', video_factory):
        qt, window, app, _ = build_app(profile, connect_relay=False, dry_hardware=True)
    assert window._teleop.dry_run and agent.dry_run
    window._teleop.mate_com_applied = True  # Only this isolated dry hardware window.
    window._on_mate_applied_changed(True)
    window.mate_panel.hide()
    window.setWindowTitle('YAM Operator - FLOW TEST - NO ARM MOTION')
    banner = QLabel('FLOW TEST | No Mate required | Real YAM video | Arms and Home simulated')
    banner.setStyleSheet('background:#fff1b8;color:#3b2f00;padding:10px;font-weight:bold;')
    window.centralWidget().layout().insertWidget(0, banner)
    window._relay_lbl.setText('Public video / local flow test')
    window.show()
    state = {'pid': os.getpid(), 'work': str(work), 'url': url,
             'physical_motion': False, 'public_state_modified': False,
             'real_video_sessions': {k: v for k, v in robot['sessions'].items() if k.startswith('video')},
             'steps': [], 'verified': False}
    (HERE / '.build/operator-flow-current.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    stage = 'ready'
    active_since = 0.
    def tick():
        nonlocal stage, active_since
        agent._maybe_announce_ready()
        if agent._state_bridge._client.connected and not agent._teleop_enabled:
            agent.teleop_enable()
        snap = window._state_client.get_snapshot(rid)
        phase = snap.get('phase')
        sub = snap.get('substate')
        if not state['steps'] or (phase, sub) != (state['steps'][-1]['phase'], state['steps'][-1]['substate']):
            state['steps'].append({'phase': phase, 'substate': sub})
        if args.verify and stage == 'ready' and snap.get('teleop_enabled') and snap.get('teleop_capability_confirmed'):
            window.mode_tab_bar.set_mode(OperatorMode.TELEOP)
            window._on_teleop_list_start(rid)
            stage = 'starting'
        elif stage == 'starting' and sub == 'teleop_active':
            active_since = time.monotonic()
            stage = 'active'
        elif stage == 'active' and time.monotonic() - active_since > 2:
            window._on_teleop_stop(rid)
            stage = 'stopping'
        elif stage == 'stopping' and phase == 'IDLE':
            state['verified'] = True
            stage = 'done'
            banner.setText('FLOW TEST PASSED | Click Teleop to repeat | Real video; no arm motion')
        (work / 'report.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(300)
    try:
        qt.exec()
    finally:
        window._cam_third.stop()
        agent.close()
        server.should_exit = True
        worker.join(timeout=5)


if __name__ == '__main__':
    main()
