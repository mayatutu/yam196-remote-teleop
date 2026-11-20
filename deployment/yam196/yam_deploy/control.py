"""Explicit, fail-closed transitions; no automatic evaluation restoration."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from . import runtime

AGENT = 'yam-teleop-agent.service'
VIDEOS = [f'yam-teleop-video@{s}.service' for s in ('head', 'left', 'right')]
UNITS = [*VIDEOS, AGENT]
FOREIGN = ('eval_gui.py', 'colosseum-unified-ui-yam2', 'colosseum-policy-server',
           'ui_trial.py', 'yam2-roboeval', 'camera_server.py', 'launch_yaml',
           'yam_remote_dual_server', 'remote_teleop.home_move', 'arm_relay_server_bridge')


@contextlib.contextmanager
def transition_lock():
    import fcntl
    path = runtime.ROOT / 'runtime/switch.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another transition/profile update is running') from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class System:
    def run(self, *args):
        return subprocess.run(args, capture_output=True, text=True, timeout=45, check=True).stdout.strip()

    def unit_state(self, unit):
        out = self.run('systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState')
        result = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        if result.get('LoadState') not in ('loaded', 'not-found') or 'ActiveState' not in result:
            raise RuntimeError(f'Cannot determine service state: {unit}')
        return result

    def active(self, unit):
        return self.unit_state(unit)['ActiveState'] not in ('inactive', 'failed')

    def start(self, unit):
        self.run('systemctl', '--user', 'start', unit)
        if not self.active(unit):
            raise RuntimeError(f'Service did not stay active: {unit}')

    def stop(self, unit):
        self.run('systemctl', '--user', 'stop', unit)
        if self.active(unit):
            raise RuntimeError(f'Service did not stop: {unit}')

    def processes(self):
        result = []
        for p in Path('/proc').iterdir():
            if not p.name.isdigit() or int(p.name) == os.getpid():
                continue
            try:
                args = (p / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                result.append((int(p.name), args))
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                raise RuntimeError(f'Cannot inspect process {p.name}; refusing handoff') from exc
        return result

    def blockers(self, *, include_managed=False, include_video=True):
        result = []
        for pid, args in self.processes():
            managed = str(runtime.ROOT) + '/' in args
            if managed:
                patterns = ('yam_remote_dual_server', 'remote_teleop.home_move', 'arm_relay_server_bridge')
                if include_video:
                    patterns += ('low-latency-teleop/sender',)
                if include_managed and any(x in args for x in patterns):
                    result.append(pid)
                continue
            if any(x in args for x in FOREIGN):
                result.append(pid)
        return result

    def independent_blockers(self):
        # Evaluation processes are expected before stop; independent trials are not.
        return [pid for pid, args in self.processes()
                if any(x in args for x in ('ui_trial.py', 'yam2-roboeval', 'camera_server.py',
                                           'launch_yaml', 'yam_remote_dual_server'))
                and str(runtime.ROOT) + '/' not in args]

    def eval_idle(self, cfg):
        req = urllib.request.Request(cfg['eval_state_url'])
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.load(response)
        # Never infer idle from absent keys or HTTP failure.
        return (data.get('run', {}).get('phase') == 'idle'
                and data.get('recording') is False and data.get('preview') is False)

    def ipc(self, command):
        from remote_teleop.teleop_ipc import default_socket_path, send_command
        return send_command(default_socket_path('SZ_YAM02'), command, timeout_sec=5)

    def wait_ready(self, since):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if not all(self.active(u) for u in UNITS):
                raise RuntimeError('Teleop service exited during startup')
            try:
                status = self.ipc('status')
                from .video import health_ready
                if status.get('ok') and status.get('state_synced') and health_ready(since):
                    return
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(.5)
        raise RuntimeError('Timed out waiting for live video and State synchronization')


def preflight(system=None):
    system = system or System()
    cfg = runtime.config()
    errors = []
    try:
        runtime.require_commissioned()
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(str(exc))
    units = {u: system.unit_state(u) for u in [*cfg['eval_units'], *UNITS]}
    blockers = system.independent_blockers()
    if blockers:
        errors.append(f'Independent device processes: {blockers}')
    return {'ok': not errors, 'mode': runtime.state()['mode'], 'errors': errors,
            'units': units, 'independent_pids': blockers, 'hardware_probed': False}


def to_teleop(system=None):
    _start(system, standby=False)


def to_standby(system=None):
    _start(system, standby=True)


def retry_standby(system=None):
    _start(system, standby=True, retry=True)


def _start(system, *, standby, retry=False):
    system = system or System()
    with transition_lock():
        # Validate before any evaluation stop/state mutation. Standby never enables motion.
        cfg = runtime.require_state_connection() if standby else runtime.require_commissioned()
        old = runtime.state()
        if retry:
            if old['mode'] != 'fault':
                raise RuntimeError('Standby retry requires a latched fault')
            if (any(system.active(u) for u in cfg['eval_units'])
                    or system.blockers(include_managed=True)):
                raise RuntimeError('Standby retry requires all device owners to be stopped')
        elif old['mode'] == 'fault':
            raise RuntimeError('Fault is latched; inspect it and explicitly recover with to-eval')
        if not retry and old['mode'] not in ('installed', 'eval'):
            raise RuntimeError(f'Cannot switch from {old["mode"]}')
        if any(system.active(u) for u in UNITS):
            raise RuntimeError('Teleop services already active outside a completed transition')
        if system.independent_blockers():
            raise RuntimeError('Independent trial/device process blocks switching')
        previous = (old.get('previous_eval', []) if retry else
                    [u for u in cfg['eval_units'] if system.active(u)])
        if not retry and previous and not system.eval_idle(cfg):
            raise RuntimeError('Evaluation must be idle with preview and recording off')
        snapshot = {'mode': 'standby-starting' if standby else 'starting',
                    'previous_eval': previous, 'started': [], 'fault': None}
        from .configure import render
        render(runtime.ROOT)
        # A previous head_only profile must not silently carry into a fresh session.
        (runtime.ROOT / 'runtime').mkdir(parents=True, exist_ok=True)
        (runtime.ROOT / 'runtime/video-profile.json').write_text('{"profile":"normal"}\n')
        runtime.write_state(snapshot)
        started = []
        try:
            for unit in previous:
                system.stop(unit)
            if system.blockers(include_managed=True):
                raise RuntimeError('Residual device processes remain after evaluation stop')
            since = time.time()
            for unit in UNITS:
                started.append(unit)  # Include partially started units in cleanup.
                snapshot['started'] = started.copy()
                runtime.write_state(snapshot)
                system.start(unit)
            system.wait_ready(since)
            if standby:
                reply = system.ipc('status')
                if (not reply.get('ok') or reply.get('teleop_enabled') is not False
                        or reply.get('active_session_id') or reply.get('session_id')
                        or reply.get('teleop_session_id') or reply.get('home_running')
                        or reply.get('arm_stack_running')):
                    raise RuntimeError('Agent is not in motion-disabled standby')
            else:
                reply = system.ipc('enable')
                if not reply.get('ok') or not reply.get('teleop_enabled'):
                    raise RuntimeError('Agent did not enable Teleop capability')
            runtime.write_state({**snapshot, 'mode': 'standby' if standby else 'teleop'})
        except BaseException as exc:
            cleanup = []
            for unit in reversed(started):
                try:
                    system.stop(unit)
                except Exception as stop_exc:
                    cleanup.append(f'{unit}: {stop_exc}')
            runtime.write_state({**snapshot, 'mode': 'fault', 'fault': str(exc), 'cleanup_errors': cleanup})
            raise


def to_eval(system=None):
    system = system or System()
    with transition_lock():
        snapshot = runtime.state()
        if snapshot['mode'] not in ('teleop', 'fault', 'starting', 'standby', 'standby-starting'):
            raise RuntimeError('No previous Teleop transition to restore')
        if system.active(AGENT):
            status = system.ipc('status')
            if (not status.get('ok') or status.get('active_session_id') or status.get('session_id') or
                    status.get('teleop_session_id') or status.get('home_running') or status.get('arm_stack_running')):
                raise RuntimeError('End the active session/Home before switching to evaluation')
            reply = system.ipc('disable')
            if not reply.get('ok'):
                raise RuntimeError('Agent capability disable failed')
        runtime.write_state({**snapshot, 'mode': 'stopping'})
        restored = []
        try:
            for unit in reversed(UNITS):
                system.stop(unit)
            if system.blockers(include_managed=True):
                raise RuntimeError('Device ownership is not released')
            restore = set(snapshot.get('previous_eval', []))
            for unit in reversed(runtime.config()['eval_units']):
                if unit in restore:
                    restored.append(unit)
                    system.start(unit)
            runtime.write_state({'mode': 'eval', 'previous_eval': [], 'fault': None})
        except BaseException as exc:
            cleanup = []
            for unit in reversed(restored):
                try:
                    system.stop(unit)
                except Exception as stop_exc:
                    cleanup.append(str(stop_exc))
            runtime.write_state({**snapshot, 'mode': 'fault', 'fault': str(exc), 'cleanup_errors': cleanup})
            raise
