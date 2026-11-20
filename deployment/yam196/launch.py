#!/usr/bin/env python3
"""Deployment entrypoint. status/preflight do not open CAN or cameras."""
from pathlib import Path
import argparse
import json
import os
import sys

RELEASE = Path(__file__).resolve().parent
sys.path[:0] = [str(RELEASE / 'python'), str(RELEASE / 'arena-client/yam_client'),
                str(RELEASE / 'piper-mate/Python_SDK')]
from yam_deploy import runtime


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['status', 'preflight', 'to-teleop', 'to-standby', 'retry-standby', 'to-eval', 'mirror-test-start', 'mirror-test-stop', 'agent', 'video', 'profile', 'guard'])
    p.add_argument('value', nargs='?')
    args = p.parse_args()
    if args.command == 'status':
        from yam_deploy.control import System, UNITS
        system = System()
        print(json.dumps({'state': runtime.state(), 'enabled': runtime.config()['enabled'],
                          'units': {u: system.unit_state(u) for u in UNITS}}, indent=2))
    elif args.command == 'preflight':
        from yam_deploy.control import preflight
        report = preflight()
        print(json.dumps(report, indent=2))
        return 0 if report['ok'] else 2
    elif args.command in ('to-teleop', 'to-standby', 'retry-standby', 'to-eval'):
        from yam_deploy import control
        getattr(control, args.command.replace('-', '_'))()
    elif args.command in ('mirror-test-start', 'mirror-test-stop'):
        from yam_deploy.mirror_test import start, stop
        (start if args.command.endswith('start') else stop)()
    elif args.command == 'guard':
        runtime.require_running(args.value or 'runtime')
    elif args.command == 'agent':
        runtime.require_running('agent')
        os.environ['OPERATOR_STATE_TOKEN'] = runtime.token()
        from remote_teleop.arena_agent import main as run
        agent_config = 'config/mirror-test/agent.yaml' if runtime.state()['mode'].startswith('mirror-test') else 'config/agent.yaml'
        return run(['--config', str(runtime.ROOT / agent_config),
                    '--client-root', str(runtime.ROOT / 'runtime/arena-client')])
    elif args.command == 'video':
        runtime.require_running('video')
        from yam_deploy.video import command
        if args.value not in ('head', 'left', 'right'):
            raise ValueError('Expected head, left or right')
        cmd = command(args.value)
        os.execv(cmd[0], cmd)
    elif args.command == 'profile':
        from yam_deploy.video import set_profile
        set_profile(args.value)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'yam-teleop: {exc}', file=sys.stderr)
        raise SystemExit(2)
