"""No device initialization or external network calls in configuration checks."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path('/home/ai/yam-teleop')
COMMISSIONING = ('mate_mapping', 'home', 'cameras', 'public_network', 'device_release')


def read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError(f'Expected object: {path}')
    return result


def config() -> dict:
    cfg = read_json(ROOT / 'config/station.json')
    if cfg.get('robot_id') != 'SZ_YAM02':
        raise ValueError('Wrong robot identity')
    arms = cfg['arms']
    if [arms[s]['channel'] for s in ('left', 'right')] != ['can_left', 'can_right']:
        raise ValueError('Unexpected CAN assignment')
    serials = [c['serial'] for c in cfg['cameras'].values()]
    if len(serials) != 3 or len(set(serials)) != 3 or not all(serials):
        raise ValueError('Three distinct SDK camera serials are required')
    return cfg


def state() -> dict:
    p = ROOT / 'runtime/mode.json'
    return read_json(p) if p.exists() else {'mode': 'installed', 'previous_eval': []}


def write_state(value: dict) -> None:
    dest = ROOT / 'runtime/mode.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, dest)


def token() -> str:
    p = ROOT / 'config/state.token'
    if not p.is_file():
        raise RuntimeError('State authentication is not configured')
    if os.name == 'posix' and p.stat().st_mode & 0o077:
        raise RuntimeError('state.token permissions must be 0600')
    value = p.read_text().strip()
    if not value or value.startswith(('CHANGE_', '${', 'TODO')):
        raise RuntimeError('State authentication is not configured')
    return value


def require_state_connection() -> dict:
    cfg = config()
    token()
    if cfg.get('state_enabled') is not True:
        raise RuntimeError('State connection is disabled')
    return cfg


def require_commissioned() -> dict:
    cfg = config()
    if cfg.get('enabled') is not True:
        raise RuntimeError('Teleop is installed but disabled; real commissioning is required')
    missing = [k for k in COMMISSIONING if cfg.get('commissioning', {}).get(k) is not True]
    if missing:
        raise RuntimeError('Uncommissioned: ' + ', '.join(missing))
    token()
    if cfg.get('state_enabled') is not True:
        raise RuntimeError('State connection is disabled')
    mapping = cfg.get('server_mapping')
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError('Mate server_mapping is not configured')
    for side in ('left', 'right'):
        for suffix in ('joint_signs', 'joint_scales', 'joint_offsets',
                       'joint_rate_limit_rad_s', 'align_rate_limit_rad_s'):
            import math
            values = [float(x) for x in str(mapping.get(f'{side}_{suffix}', '')).split(',') if x]
            if len(values) != 6 or not all(math.isfinite(x) for x in values):
                raise RuntimeError(f'Invalid Mate mapping: {side}_{suffix}')
            if suffix == 'joint_signs' and any(x not in (-1, 1) for x in values):
                raise RuntimeError('Joint signs must be -1 or +1')
            if suffix.endswith('rad_s') and any(x <= 0 for x in values):
                raise RuntimeError('Motion rate limits must be positive')
    for side in ('left', 'right'):
        target = cfg['arms'][side].get('home')
        import math
        if not isinstance(target, list) or len(target) != 7 or not all(math.isfinite(x) for x in target):
            raise RuntimeError(f'{side} Home has not been configured')
    return cfg


def require_running(kind: str) -> dict:
    current = state()
    if current.get('mode') in ('mirror-test-starting', 'mirror-test'):
        if kind not in ('agent', 'video', 'enable', 'handoff', 'server', 'bridge', 'robot', 'video profile'):
            raise RuntimeError(f'{kind}: unavailable in one-Mate test; Home is prohibited')
        from .mirror_test import require_profile
        cfg = require_profile(current)
    elif current.get('mode') in ('standby-starting', 'standby'):
        if kind not in ('agent', 'video'):
            raise RuntimeError(f'{kind}: motion and capability enable are blocked in standby')
        cfg = require_state_connection()
    else:
        cfg = require_commissioned()
        if current.get('mode') not in ('starting', 'teleop'):
            raise RuntimeError(f'{kind}: no active Teleop ownership; use teleopctl to-teleop')
    # A controller lock does not exclude SocketCAN writers. Check existing services
    # and independent launchers again immediately before claiming hardware/network.
    from .control import System
    system = System()
    if any(system.active(u) for u in cfg['eval_units']):
        raise RuntimeError(f'{kind}: evaluation services are still active')
    blockers = system.blockers(include_managed=False)
    if blockers:
        raise RuntimeError(f'{kind}: unmanaged device processes: {blockers}')
    return cfg
