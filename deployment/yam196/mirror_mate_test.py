"""Dedicated one-Mate/two-output test client. Normal dual client is never changed."""
from pathlib import Path
import argparse
import hashlib
import sys
from types import ModuleType

SDK = Path('E:/working/YAM/YAM/arena-client/dist/YAM Operator-video-health/runtime/piper_sdk')


def mirror_source(source: str) -> str:
    changes = [
        ('    right_reader = PiperMateReader(args.right_pipermate, ARM_NAME_RIGHT, args.gripper)',
         '    right_reader = None  # TEST: one serial owner and one read loop only'),
        ('    left_state = ArmInputState()\n    right_state = ArmInputState()',
         '    left_state = ArmInputState(input_error=True)\n    right_state = left_state'),
        ('    right_thread = threading.Thread(\n        target=read_loop,\n'
         '        args=(right_reader, right_state, state_lock, stop_event),\n'
         '        name="ReadLoopRight",\n        daemon=True,\n    )',
         '    right_thread = None'),
        ('    right_thread.start()', '    # TEST: shared snapshot feeds both independently sequenced PMRC blocks'),
        ('                right_joints = dict(right_state.joints_deg)',
         '                right_joints = dict(right_state.joints_deg)\n'
         '                input_pending = left_state.read_count == 0 and not left_state.error_message\n'
         '            if input_pending:\n'
         '                # No input is different from a read fault: send nothing until a real sample.\n'
         '                time.sleep(min(interval, 0.01))\n'
         '                next_tick = time.perf_counter()\n'
         '                continue'),
        ('        right_thread.join(timeout=1.0)', '        # TEST: no second reader thread to join'),
        ('        right_reader.close()', '        # TEST: shared serial port is closed once above'),
    ]
    for old, new in changes:
        if source.count(old) != 1:
            raise RuntimeError('SDK test adapter no longer matches; refusing to start')
        source = source.replace(old, new)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mirror-test', action='store_true', required=True)
    parser.add_argument('--mate', required=True)
    parser.add_argument('--prepare-only', action='store_true')
    args, remaining = parser.parse_known_args()
    if any(x in remaining for x in ('--left-pipermate', '--right-pipermate')):
        parser.error('Mirror test uses only --mate; do not supply two ports')
    source = mirror_source((SDK / 'remote_dual_client.py').read_text(encoding='utf-8'))
    compile(source, '<one-Mate mirror test>', 'exec')
    if args.prepare_only:
        print('Mirror test adapter ready; no serial port opened; source SHA256=' +
              hashlib.sha256(source.encode()).hexdigest())
        return
    print('TEST ONLY: ONE MATE DRIVES BOTH YAM ARMS. Normal client/profile is unchanged.', flush=True)
    sys.path.insert(0, str(SDK))
    sys.argv = ['mirror-test-client', '--left-pipermate', args.mate,
                '--right-pipermate', 'MIRROR_TEST', *remaining]
    module = ModuleType('yam_mirror_test_client')
    module.__file__ = str(SDK / 'remote_dual_client.py')
    sys.modules[module.__name__] = module
    exec(compile(source, '<one-Mate mirror test>', 'exec'), module.__dict__)
    module.main()


if __name__ == '__main__':
    main()
