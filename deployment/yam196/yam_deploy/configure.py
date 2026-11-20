"""Render existing upstream config formats from the local station settings."""
from pathlib import Path
import yaml
from . import runtime


def render(root: Path):
    cfg = runtime.read_json(root / 'config/station.json')
    rid = cfg['robot_id']
    current = root / 'current'
    sdk = str(current / 'piper-mate/Python_SDK')
    working = root / 'runtime/arena-client'
    working.mkdir(parents=True, exist_ok=True)
    paths = {s: str(root / f'config/{s}.yaml') for s in ('left', 'right')}
    for side in paths:
        arm = cfg['arms'][side]
        value = {'robot': {'_target_': 'gello_min.yam.YAMRobot', 'channel': arm['channel']},
                 'agent': {'start_joints': arm['home']}}
        Path(paths[side]).write_text(yaml.safe_dump(value), encoding='utf-8')
    server = {'bind_ip': '127.0.0.1', 'port': 5012, 'left_can': 'can_left', 'right_can': 'can_right',
              'allow_single_arm': False, 'run_preinit': False, 'auto_resume': False,
              'stale_hold_ms': 150, 'hard_timeout_ms': 600, 'mapping_mode': 'absolute',
              'debug_run_dir': str(root / 'logs/arm-debug')}
    allowed = {f'{side}_{key}' for side in ('left','right') for key in
               ('joint_signs','joint_scales','joint_offsets','joint_rate_limit_rad_s',
                'align_rate_limit_rad_s','gripper_scale','gripper_offset','gripper_min','gripper_max')}
    mapping = cfg.get('server_mapping') or {}
    if set(mapping) - allowed:
        raise ValueError('Unsupported server_mapping keys')
    server.update(mapping)
    (root / 'config/server.yaml').write_text(yaml.safe_dump(server), encoding='utf-8')
    remote = {'enabled': True, 'piper_mate_sdk': sdk, 'low_latency_sdk': sdk,
              'relay': {'host': cfg['relay_host'], 'arm_port': cfg['relay_arm_port'],
                        'video_port': cfg['relay_video_port'], 'arm_session': f'yam_arm_{rid}',
                        'agent_control_session': f'arena_agent_ctrl_{rid}'},
              'robot': {'yam_remote_server_bind_ip': '127.0.0.1', 'yam_remote_server_port': 5012,
                        'left_can': 'can_left', 'right_can': 'can_right',
                        'yam_server_config': str(root / 'config/server.yaml')},
              'safety': {'require_can_handoff_preflight': True, 'require_home_after_teleop': True,
                         'can_release_grace_sec': 2.0},
              'state': {'enabled': cfg['state_enabled'], 'server': cfg['state_url'],
                        'websocket': cfg['state_url'].replace('http', 'ws', 1),
                        'token': '${OPERATOR_STATE_TOKEN}'}}
    value = {'robot_id': rid, 'client_root': str(working),
             'left_config_path': paths['left'], 'right_config_path': paths['right'],
             'remote_teleop': remote}
    (root / 'config/agent.yaml').write_text(yaml.safe_dump(value), encoding='utf-8')
