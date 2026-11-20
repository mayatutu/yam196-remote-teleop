"""Real local State/Operator-client/Agent handshake with Agent hardware dry-run.

Offline unless --run. Uses an isolated database, random test robot, loopback
HTTP/WebSocket and the existing production classes. This does not validate
public State, .196 Agent, Mate, physical Home, arm data or video decoding.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
SOURCE = Path('E:/working/YAM/YAM/arena-client/yam_client')


def main():
    work = HERE/'.build'/('state-session-'+uuid.uuid4().hex[:12])
    work.mkdir()
    os.chdir(work)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    sys.path.insert(0, str(SOURCE))
    # Importing the existing State entrypoint creates a default app as well;
    # keep its unused default database inside this task's own artifact folder.
    import uvicorn
    import yaml
    from PySide6.QtWidgets import QApplication
    from operator_state.config import RobotEntry, StateServerConfig
    from operator_state.main import create_app
    from remote_teleop.arena_agent import ArenaAgent
    from remote_teleop.phase import RemoteTeleopConfig
    from yam_operator.config.app_config import AppConfig, StateConfig
    from yam_operator.core.app_state import AppState
    from yam_operator.core.arena_client import ArenaClient
    from yam_operator.core.state_adapter import StateAdapter
    from yam_operator.core.state_client import StateClient
    from yam_operator.core.state_sync import StateSync
    from yam_operator.ui.main_window import MainWindow

    rid = 'SYN_'+uuid.uuid4().hex[:10]
    token = secrets.token_urlsafe(24)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    url = 'http://127.0.0.1:'+str(port)
    wsurl = url.replace('http://', 'ws://')
    cfg = StateServerConfig(host='127.0.0.1', port=port, token=token,
                            sqlite_path=work/'state.db',
                            robots=[RobotEntry(robot_id=rid, arm_session='syn_'+uuid.uuid4().hex[:20])])
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host='127.0.0.1',
                                          port=port, log_level='error', access_log=False))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'robot_id': rid,
              'scope': 'local real HTTP/WebSocket, Agent dry-run, Operator production client/adapter/UI',
              'physical_devices': False, 'public_state_tested': False,
              'remote_files_written': False, 'steps': [], 'passed': False}
    agent = client = window = None
    qt = QApplication.instance() or QApplication([])
    try:
        worker.start()
        deadline = time.monotonic()+8
        while not server.started:
            if time.monotonic()>deadline: raise TimeoutError('Local State startup')
            time.sleep(.02)
        agent_cfg = work/'agent.yaml'
        agent_cfg.write_text(yaml.safe_dump({'robot_id': rid, 'remote_teleop': {
            'state': {'enabled': True, 'server': url, 'token': '${YAM_E2E_LOCAL_TOKEN}'}}}), encoding='utf-8')
        os.environ['YAM_E2E_LOCAL_TOKEN'] = token
        # Its compatibility UDP endpoint is loopback-only and is not run.
        agent = ArenaAgent(config_path=str(agent_cfg), python=sys.executable,
                           client_root=work, relay_host='127.0.0.1', relay_port=port,
                           agent_session='syn_'+uuid.uuid4().hex[:20], robot_id=rid,
                           teleop_config=RemoteTeleopConfig(), dry_run=True)
        agent._state_bridge.start()
        app = AppState(local_client_id='operator-e2e-'+uuid.uuid4().hex[:8])
        app.register_robot(rid)
        sync = StateSync(app)
        client = StateClient(server=url, websocket=wsurl, token=token,
                             client_id=app.local_client_id, on_snapshot=sync.apply_snapshot)
        config = AppConfig(server=url, relay_host='127.0.0.1', relay_control_port=port,
                           relay_video_port=port, robots=(),
                           state=StateConfig(enabled=True, server=url, websocket=wsurl,
                                             token=token, fallback_relay_control=False))
        adapter = StateAdapter(config, ArenaClient(), client, sync)
        client.start()

        def wait(label, predicate, timeout=10):
            until = time.monotonic()+timeout
            while True:
                client.drain_inbox()
                qt.processEvents()
                agent._maybe_announce_ready()
                snap = client.get_snapshot(rid)
                if predicate(snap):
                    report['steps'].append({'step': label, 'phase': snap.get('phase'),
                                             'substate': snap.get('substate')})
                    return snap
                if time.monotonic()>until:
                    raise TimeoutError(label+': '+json.dumps({k:snap.get(k) for k in
                        ('phase','substate','arena_ready','agent_online','teleop_enabled')}))
                time.sleep(.03)

        wait('agent_connected_idle', lambda s: s.get('agent_online') and s.get('arena_ready')
             and client.connected)
        status = agent.teleop_enable()
        assert status.get('teleop_enabled') is True, status
        wait('capability_confirmed', lambda s: s.get('teleop_capability_confirmed')
             and s.get('teleop_enabled'))
        response = adapter.teleop_start(rid)
        session = response['snapshot']['session_id']
        wait('agent_prepared_dry_stack', lambda s:s.get('substate')=='teleop_client_starting')
        adapter.teleop_client_ready(rid, session)
        snap = wait('active', lambda s:s.get('substate')=='teleop_active')
        assert snap.get('owner_client_id')==app.local_client_id
        assert snap.get('mate_lock',{}).get('holder_client_id')==app.local_client_id
        sync.apply_snapshot(rid, snap)
        window = MainWindow(app_state=app, relay_client=None, teleop_manager=None)
        window.resize(1280, 820)
        window.show()
        window._refresh_all()
        qt.processEvents()
        assert window.grab().save(str(work/'operator-active.png'))
        adapter.teleop_stop(rid, session)
        final = wait('stopped_home_simulated_idle', lambda s:s.get('phase')=='IDLE'
                     and not s.get('session_id'))
        assert not final.get('mate_lock',{}).get('holder_client_id')
        report['passed'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
    finally:
        if window is not None: window.close()
        if client is not None: client.close()
        if agent is not None: agent.close()
        server.should_exit = True
        worker.join(timeout=8)
        listener.close()
        os.environ.pop('YAM_E2E_LOCAL_TOKEN', None)
        report['server_thread_exited'] = not worker.is_alive()
        report['passed'] = report['passed'] and report['server_thread_exited']
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (work/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report, indent=2))
        print('Artifacts: '+str(work))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if args.run:
        raise SystemExit(main())
    print('Offline preflight: State, client and Agent sources exist; no connections started.')
    assert (SOURCE/'operator_state/main.py').is_file()
    assert (SOURCE/'remote_teleop/arena_agent.py').is_file()
