"""Offline installation acceptance: simulation, fake video and loopback only."""
from __future__ import annotations
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time


def verify(release, before_units):
    root = Path('/home/ai/yam-teleop')
    sys.path[:0] = [str(release / 'python'), str(release / 'arena-client/yam_client'),
                    str(release / 'piper-mate/Python_SDK')]
    modules = ['yam_deploy.calibration','remote_teleop.arena_agent','remote_teleop.home_move',
               'gello_min.yam','yam_remote_dual_server','arm_relay_server_bridge',
               'i2rt.robots.get_robot','omegaconf','websocket']
    for name in modules: importlib.import_module(name)
    import i2rt
    if not Path(i2rt.__file__).resolve().is_relative_to(release):
        raise RuntimeError('Driver resolves outside isolated release')
    from i2rt.robots.get_robot import get_yam_robot
    simulated = get_yam_robot(sim=True)
    assert len(simulated.get_joint_pos()) == 7
    close = getattr(simulated, 'close', None)
    if close: close()
    sender = release / 'low-latency-teleop/sender'
    linked = subprocess.check_output(['ldd', str(sender)], text=True)
    if 'not found' in linked or 'librealsense2' not in linked or 'libavcodec.so.58' not in linked:
        raise RuntimeError('Sender dynamic dependencies are incomplete')
    symbols = subprocess.check_output(['nm', '-D', str(sender)], text=True)
    if 'rs2_pipeline_start_with_config' not in symbols:
        raise RuntimeError('Sender contains no real RealSense capture implementation')
    report = {'imports': modules, 'simulated_dofs': 7, 'real_hardware_opened': False,
              'sender_sha256': hashlib.sha256(sender.read_bytes()).hexdigest(), 'fake_video': []}
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(sys.path[:3])
    # Source guard tests must fail BEFORE evaluating supplied hardware arguments.
    for args in ([str(release / 'launch.py'), 'guard', 'video'],
                 [str(release / 'piper-mate/Python_SDK/yam_remote_dual_server.py')],
                 ['-m', 'remote_teleop.home_move', '--left-config-path', '/nonexistent']):
        result = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 or 'disabled' not in result.stderr + result.stdout:
            raise RuntimeError(f'Real startup gate did not reject: {args[0]}')
    # C sender guard also fails with no current link (127) before final promotion.
    result = subprocess.run([str(sender), 'realsense:DO_NOT_OPEN', 'libx265'], capture_output=True, timeout=10)
    if result.returncode != 2:
        raise RuntimeError('Native video startup gate did not reject')
    with tempfile.TemporaryDirectory(prefix='fake-video-', dir=root / 'runtime') as temp:
        processes = []
        receivers = []
        files = []
        try:
            for role in ('head','left','right'):
                receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                receiver.bind(('127.0.0.1',0)); receiver.settimeout(.25)
                receivers.append(receiver)
                health = Path(temp) / f'{role}.json'
                log = (Path(temp) / f'{role}.log').open('wb'); files.append(log)
                cmd = [str(sender), '--mode','relay','--relay-ip','127.0.0.1',
                       '--relay-port',str(receiver.getsockname()[1]),'--session',f'test_{role}',
                       '--width','160','--height','120','--fps','10','--bitrate','100000',
                       '--health-file',str(health),'fake','libx265']
                processes.append(subprocess.Popen(cmd, stdout=log, stderr=log))
            counts = [0,0,0]
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                for i, receiver in enumerate(receivers):
                    try:
                        receiver.recvfrom(65535); counts[i]+=1
                    except socket.timeout: pass
            for i, role in enumerate(('head','left','right')):
                data = json.loads((Path(temp)/f'{role}.json').read_text())
                if processes[i].poll() is not None or counts[i] == 0 or data['encoded_total'] < 1 or data['session'] != f'test_{role}':
                    raise RuntimeError(f'Fake video failed: {role}')
                report['fake_video'].append({'role':role,'packets':counts[i],'frames':data['encoded_total']})
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill();proc.wait()
            for f in files: f.close()
            for receiver in receivers: receiver.close()
    if not before_units:
        from yam_deploy.control import UNITS
        for unit in [*UNITS,'yam-teleop.target']:
            active = subprocess.run(['systemctl','--user','is-active',unit],capture_output=True,text=True)
            enabled = subprocess.run(['systemctl','--user','is-enabled',unit],capture_output=True,text=True)
            if active.stdout.strip() != 'inactive' or enabled.stdout.strip() != 'disabled':
                raise RuntimeError(f'New unit must be disabled/inactive: {unit}')
        report['new_services'] = 'disabled/inactive'
    (root / 'logs' / ('offline-validation-pre.json' if before_units else 'offline-validation.json')).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);p.add_argument('--before-units',action='store_true')
    args=p.parse_args();verify(args.release.resolve(),args.before_units)
