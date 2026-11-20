"""Three owned camera senders; old wrist-profile interface is retained."""
import json
import os
from pathlib import Path
import time

from . import runtime
from .control import System, transition_lock


def profile():
    p = runtime.ROOT / 'runtime/video-profile.json'
    return runtime.read_json(p)['profile'] if p.exists() else 'normal'


def command(role, *, selected=None):
    cfg = runtime.config()
    cam = cfg['cameras'][role]
    mode = selected or profile()
    if mode not in ('normal', 'reduced', 'head_only'):
        raise ValueError('Unknown video profile')
    if mode == 'head_only' and role != 'head':
        raise RuntimeError('Wrist cameras are disabled by head_only profile')
    fps, bitrate = cam['fps'], cam['bitrate']
    if role != 'head' and mode == 'reduced':
        fps, bitrate = 15, 180000
    return [str(runtime.ROOT / 'current/low-latency-teleop/sender'),
            '--mode', 'relay', '--relay-ip', cfg['relay_host'],
            '--relay-port', str(cfg['relay_video_port']), '--session', cam['session'],
            '--width', str(cam['width']), '--height', str(cam['height']),
            '--capture-fps', str(cam['capture_fps']), '--fps', str(fps),
            '--bitrate', str(bitrate), '--health-file',
            str(runtime.ROOT / f'runtime/video-{role}.json'),
            'realsense:' + cam['serial'], 'libx265']


def health_ready(since, roles=('head', 'left', 'right')):
    cfg = runtime.config()
    now = time.time()
    for role in roles:
        p = runtime.ROOT / f'runtime/video-{role}.json'
        if not p.exists() or p.stat().st_mtime < since or now - p.stat().st_mtime > 4:
            return False
        data = runtime.read_json(p)
        if (data.get('session') != cfg['cameras'][role]['session'] or
                data.get('encoded_total', 0) <= 0 or data.get('encoded_fps', 0) <= 0):
            return False
    return True


def set_profile(selected, system=None):
    if selected not in ('normal', 'reduced', 'head_only'):
        raise ValueError('Unknown video profile')
    system = system or System()
    with transition_lock():
        runtime.require_running('video profile')
        current = runtime.state()
        since = time.time()
        try:
            for role in ('left', 'right'):
                system.stop(f'yam-teleop-video@{role}.service')
            p = runtime.ROOT / 'runtime/video-profile.json'
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'profile': selected}) + '\n')
            os.replace(tmp, p)
            if selected != 'head_only':
                for role in ('left', 'right'):
                    system.start(f'yam-teleop-video@{role}.service')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if health_ready(since, ('left', 'right')):
                        return
                    time.sleep(.3)
                raise RuntimeError('Wrist video did not become healthy')
        except BaseException as exc:
            # Do not pretend the requested profile was successfully applied.
            runtime.write_state({**current, 'mode': 'fault', 'fault': f'video profile: {exc}'})
            raise
