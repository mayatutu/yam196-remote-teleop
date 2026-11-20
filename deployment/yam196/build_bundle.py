"""Freeze only the required local sources, then apply reviewed deployment adapters.

Never edits the source YAM workspace. Rebuild .build/release from immutable evidence.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import textwrap

HERE = Path(__file__).resolve().parent
BUILD = HERE / '.build'
RELEASE = BUILD / 'release'
ORIGINS = {}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy(source, relative):
    dest = RELEASE / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    ORIGINS[str(relative).replace('\\', '/')] = {'source': str(source), 'sha256': sha(source)}


def edit(relative, old, new):
    p = RELEASE / relative
    s = p.read_text(encoding='utf-8')
    if s.count(old) != 1:
        raise RuntimeError(f'Patch requires exactly one match: {relative}: {old[:80]}')
    p.write_text(s.replace(old, new), encoding='utf-8', newline='\n')


def function(relative, name, code):
    p = RELEASE / relative
    s = p.read_text(encoding='utf-8')
    node = next(n for n in ast.parse(s).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = s.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno] = [textwrap.dedent(code).strip() + '\n']
    p.write_text(''.join(lines), encoding='utf-8', newline='\n')


def build(source):
    if RELEASE.exists():
        # Only this builder's reproducible output, never a source or installed release.
        if RELEASE.resolve() != (HERE / '.build/release').resolve():
            raise RuntimeError('Unexpected build output')
        shutil.rmtree(RELEASE)
    RELEASE.mkdir(parents=True)
    remote = ['__init__','arena_agent','agent_state_bridge','state_agent','arm_stack','all_stop','phase',
              'can_handoff','process_manager','protocol','relay_control','relay_header','io_util',
              'home_move','teleop_ipc','teleop_control','wrist_video_profile']
    for name in remote:
        rel = Path(f'arena-client/yam_client/remote_teleop/{name}.py')
        copy(source / rel, rel)
    (RELEASE / 'arena-client/yam_client/remote_teleop/__init__.py').write_text(
        '"""Standalone Teleop deployment; optional evaluation modules are not imported."""\n', encoding='utf-8')
    for p in (source / 'arena-client/yam_client/gello_min').glob('*.py'):
        copy(p, p.relative_to(source))
    sdk = ['yam_remote_dual_server','remote_dual_protocol','yam_mapping','arm_relay_server_bridge',
           'relay_bridge_proto','manual_bridge','manual_envelope','test_yam_remote_dual_server']
    # Include the SDK's pure-Python manual envelope dependencies, not a hardware client.
    for name in sdk:
        rel = Path(f'piper-mate/Python_SDK/{name}.py')
        if (source / rel).is_file(): copy(source / rel, rel)
    video = source / 'low-latency-teleop'
    for p in video.iterdir():
        if p.is_file() and (p.suffix in ('.c', '.cpp', '.h') or p.name == 'Makefile'):
            copy(p, p.relative_to(source))
    media = video / 'deps/media-server'
    for p in (media / 'librtp').rglob('*'):
        if p.is_file() and (p.suffix in ('.c', '.cpp', '.h') or p.name == 'Makefile'):
            copy(p, p.relative_to(source))
    for p in media.glob('*.mk'): copy(p, p.relative_to(source))
    for name in ('LICENSE', 'LICENSE.txt'):
        if (media / name).is_file(): copy(media / name, (media / name).relative_to(source))
    for p in (HERE / 'yam_deploy').glob('*.py'): copy(p, Path('python/yam_deploy') / p.name)
    copy(HERE / 'yam_deploy/mirror_normal_server.yaml', 'python/yam_deploy/mirror_normal_server.yaml')
    for name in ('launch.py', 'install.py', 'prepare_deps.py', 'restore_cache_wheels.py', 'verify_install.py', 'README.md'):
        if (HERE / name).exists(): copy(HERE / name, name)
    # Windows launch adapters and SSH upload helpers are tested on the preparation host.
    local_tests = {'test_mirror_mate.py', 'test_mirror_operator.py', 'test_mirror_deploy.py'}
    for p in (HERE / 'tests').glob('*.py'):
        if p.name not in local_tests:
            copy(p, Path('tests') / p.name)
    copy(HERE / 'config/station.json', 'defaults/station.json')
    copy(BUILD / 'baseline.json', 'baseline.json')

    # Exact function bodies from the currently deployed evaluator; only dependency
    # closure is extracted. No import of colosseum and no shared code edits.
    src = (BUILD / 'eval_yam.py').read_text(encoding='utf-8')
    names = {'_close_arm', '_vector', '_rebase_gripper_limits', '_open_arm'}
    body = '\n\n'.join(ast.get_source_segment(src, n) for n in ast.parse(src).body
                       if isinstance(n, ast.FunctionDef) and n.name in names)
    header = ('"""Frozen helpers from .196 colosseum_client/robots/yam.py. See manifest."""\n'
              'import json\nimport logging\nimport threading\nimport numpy as np\n'
              'log = logging.getLogger(__name__)\n_CONTROL_JOIN_TIMEOUT = 5.0\n'
              '_GRIPPER_STARTUP_TOLERANCE_RAD = .02\n\n')
    (RELEASE / 'python/yam_deploy/calibration.py').write_text(header + body + '\n', encoding='utf-8')
    ORIGINS['python/yam_deploy/calibration.py'] = {'source': '/home/ai/colosseum-client/src/colosseum_client/robots/yam.py',
                                                'sha256': sha(BUILD / 'eval_yam.py'), 'extracted_functions': sorted(names)}

    server = 'piper-mate/Python_SDK/yam_remote_dual_server.py'
    function(server, 'create_robot', '''
        def create_robot(can_name, args):
            if args.dry_run:
                return MockRobot(DEFAULT_JOINT_LIMITS)
            from yam_deploy.robot import open_arm
            return open_arm(can_name)
    ''')
    function(server, 'run_preinit_if_requested', '''
        def run_preinit_if_requested(args):
            if args.run_preinit:
                raise RuntimeError('Preinitialization is prohibited in this deployment')
            if not args.dry_run:
                from yam_deploy.runtime import require_running
                require_running('server')
                if args.allow_single_arm or args.bind_ip != '127.0.0.1' or args.port != 5012 or args.left_can != 'can_left' or args.right_can != 'can_right':
                    raise RuntimeError('Real Server must use both commissioned arms and the configured loopback endpoint')
    ''')
    edit(server, '            if not args.allow_single_arm:\n                raise',
         '            if not args.allow_single_arm:\n                from yam_deploy.robot import close_many\n'
         '                close_many([a.robot for a in arms.values()] + [arm.robot])\n                raise')
    edit(server, '    arms = setup_arms(args)\n    print_binding(arms, args)\n\n    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n    sock.bind((args.bind_ip, args.port))\n    sock.settimeout(0.01)',
         '    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n    sock.bind((args.bind_ip, args.port))\n    sock.settimeout(0.01)\n'
         '    try:\n        arms = setup_arms(args)\n    except BaseException:\n        sock.close()\n        raise\n'
         '    print_binding(arms, args)\n    import signal\n'
         '    def terminate(_signum, _frame):\n        raise KeyboardInterrupt\n'
         '    signal.signal(signal.SIGTERM, terminate)')
    edit(server, '        for arm in arms.values():\n            if arm.robot is not None:\n                try:\n                    arm.robot.close()\n                except Exception as exc:\n                    print(f"\\n[{arm.name}] close error: {exc}")\n        sock.close()',
         '        from yam_deploy.robot import close_many\n        try:\n            close_many([a.robot for a in arms.values()])\n        finally:\n            sock.close()')
    gy = 'arena-client/yam_client/gello_min/yam.py'
    edit(gy, 'from i2rt.robots.utils import GripperType\n', '')
    edit(gy, '        from i2rt.robots.get_robot import get_yam_robot\n\n        self.robot = get_yam_robot(channel=channel, gripper_type=GripperType.LINEAR_4310)',
         '        from yam_deploy.robot import open_arm\n\n        self.robot = open_arm(channel)')
    home = 'arena-client/yam_client/remote_teleop/home_move.py'
    function(home, 'run_home_move', (HERE / 'home_function.txt').read_text(encoding='utf-8'))
    handoff = 'arena-client/yam_client/remote_teleop/can_handoff.py'
    function(handoff, 'run_can_probe_subprocess', '''
        def run_can_probe_subprocess(**kwargs):
            # Passive ownership check. Does not prove CAN exclusivity or initialize motors.
            from yam_deploy.runtime import require_running
            from yam_deploy.control import System
            require_running('handoff')
            pids = System().blockers(include_managed=True, include_video=False)
            return HandoffResult(ok=not pids, reason='residual_processes' if pids else 'passive_owner_check', residual_pids=tuple(pids))
    ''')
    agent = 'arena-client/yam_client/remote_teleop/arena_agent.py'
    edit(agent, '        self.config_path = str(Path(config_path).resolve())',
         '        from yam_deploy.runtime import require_running\n        require_running("agent")\n'
         '        self.config_path = str(Path(config_path).resolve())')
    edit(agent, '    def teleop_enable(self) -> dict[str, Any]:',
         '    def teleop_enable(self) -> dict[str, Any]:\n        from yam_deploy.runtime import require_running\n        require_running("enable")')
    edit(agent, '        self._wrist_video = WristVideoProfileManager(client_root=self.client_root)',
         '        from yam_deploy.video_adapter import ProfileManager\n        self._wrist_video = ProfileManager()')
    for method, arg in [('_on_battle_start', 'payload: dict | None = None'), ('_on_scene_setup_start', 'msg: Any'), ('_on_recover_dual_arms', 'payload: dict[str, Any]')]:
        needle = f'    def {method}(self, {arg}) -> None:\n'
        edit(agent, needle, needle + '        raise RuntimeError("This deployment supports standalone Teleop only")\n')
    bridge = 'piper-mate/Python_SDK/arm_relay_server_bridge.py'
    edit(bridge, '    args = parse_args()\n', '    args = parse_args()\n    import ipaddress\n'
         '    if not ipaddress.ip_address(args.relay_ip).is_loopback:\n'
         '        from yam_deploy.runtime import require_running\n        require_running("bridge")\n')

    sender = 'low-latency-teleop/sender.c'
    edit(sender, '#include "d405_capture.h"', '#include "d405_capture.h"\n#include "deployment_guard.h"')
    edit(sender, 'strcmp(input, "d405") == 0', '(strcmp(input, "d405") == 0 || strcmp(input, "realsense") == 0)')
    edit(sender, 'int use_d405 = (strcmp(device, "d405") == 0 || strncmp(device, "d405:", 5) == 0);',
         'int use_d405 = (strcmp(device, "d405") == 0 || strncmp(device, "d405:", 5) == 0 || strcmp(device, "realsense") == 0 || strncmp(device, "realsense:", 10) == 0);\n'
         '    if (!use_fake && !yam_deployment_video_allowed()) return 2;')
    edit(sender, '        d405_cap = d405_capture_open(width, height, capture_fps, serial);',
         '        if (strncmp(device, "realsense:", 10) == 0) serial = device + 10;\n'
         '        if (!serial || !*serial) { fprintf(stderr, "explicit SDK serial required\\n"); return 2; }\n'
         '        d405_cap = d405_capture_open(width, height, capture_fps, serial);')
    # A built-in guard covers direct binary invocation, not just the service wrapper.
    copy(HERE / 'deployment_guard.h', 'low-latency-teleop/deployment_guard.h')
    edit('low-latency-teleop/Makefile', '  SENDER_EXTRA_OBJ += d405_capture_stub.o',
         '  $(error RealSense SDK development files are required for this deployment)')
    manifest = {'release': 'yam196-local-r1', 'origins': ORIGINS,
                'files': {str(p.relative_to(RELEASE)).replace('\\', '/'): sha(p)
                          for p in RELEASE.rglob('*') if p.is_file()}}
    (RELEASE / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    with tarfile.open(BUILD / 'yam196-local-r1.tar.gz', 'w:gz') as tar:
        tar.add(RELEASE, arcname='yam196-local-r1')
    print(json.dumps({'files': len(manifest['files']), 'bundle': str(BUILD / 'yam196-local-r1.tar.gz'),
                      'sha256': sha(BUILD / 'yam196-local-r1.tar.gz')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('E:/working/YAM/YAM'))
    build(parser.parse_args().source)
