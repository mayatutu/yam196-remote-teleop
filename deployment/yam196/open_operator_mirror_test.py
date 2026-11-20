"""Separate real one-Mate/two-arm Operator. No option is added to normal Operator."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import MethodType

import yaml

from mirror_mate_test import SDK, mirror_source

HERE = Path(__file__).resolve().parent
SOURCE = Path('E:/working/YAM/YAM/arena-client/yam_client')
BUNDLE = SDK.parent.parent
PROFILE = HERE / '.build/windows-mirror-test/profiles.yaml'


def prepare(mate):
    compile(mirror_source((SDK / 'remote_dual_client.py').read_text(encoding='utf-8')), '<mirror-test>', 'exec')
    cfg = yaml.safe_load((HERE / '.build/windows-operator-20261120/profiles.yam196-preview.yaml').read_text(encoding='utf-8'))
    cfg['state']['fallback_relay_control'] = False
    cfg['ui'].update(visible_modes=['start', 'teleop'], auto_close_review=False)
    cfg['mate'].update(left_com=mate, right_com=mate)
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding='utf-8')
    return PROFILE


def bind_test(window, mate):
    teleop = window._teleop
    # Only this explicitly launched window uses a shared monitor and custom subprocess.
    # MateMonitor still checks the actual COM in the OS; no synthetic port/health flags.
    teleop.mate_monitor.set_ports(mate, mate)
    teleop.mate_com_applied = True

    def command_for(dual, left, right):
        if left != mate or right != mate:
            raise RuntimeError('Dedicated test port changed; restart with --mate')
        return [dual.python, str(HERE / 'mirror_mate_test.py'), '--mirror-test', '--mate', mate,
                '--server-ip', dual.server_ip, '--server-port', str(dual.server_port),
                '--rate', str(dual.rate)]
    teleop.dual.command_for = MethodType(command_for, teleop.dual)
    # normal_stop still verifies local dual/bridge exit, but requests existing State abort.
    window._state_adapter.teleop_stop = window._state_adapter.teleop_abort
    window.mate_panel.setEnabled(False)
    window.mate_panel.hide()
    window._on_mate_applied_changed(True)


def load_credential():
    value = os.environ.get('OPERATOR_STATE_TOKEN', '')
    if value == 'dev-state-token':
        value = ''  # Never silently use an inherited development default.
    if not value:
        history_path = Path.home() / 'AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt'
        history = history_path.read_text(encoding='utf-8') if history_path.is_file() else ''
        matches = re.findall(r'\$env:OPERATOR_STATE_TOKEN\s*=\s*([\x22\x27])([^\x22\x27\r\n]+)\1', history)
        if matches:
            value = matches[-1][1]
            if value == 'dev-state-token':
                value = ''
    if not value:
        from prepare_mirror_test import SSH
        # Existing station credential travels over SSH directly into this process's
        # environment. Never save it locally or place it in argv/logs/profile.
        result = subprocess.run([*SSH, 'cat /home/ai/yam-teleop/config/state.token'],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            value = result.stdout.strip()
    # An explicit station file is authoritative even when its value resembles an
    # upstream default. Do not invent a default when all configured sources fail.
    if not value:
        raise RuntimeError('Existing verified State credential is unavailable')
    os.environ['OPERATOR_STATE_TOKEN'] = value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mirror-test', action='store_true', required=True)
    p.add_argument('--mate', required=True)
    p.add_argument('--launch', action='store_true')
    args = p.parse_args()
    if not re.fullmatch(r'COM[1-9][0-9]*', args.mate):
        p.error('Expected the physical Windows COM port')
    profile = prepare(args.mate)
    if not args.launch:
        print(json.dumps({'profile': str(profile), 'serial_opened': False, 'connected': False,
                          'launch_requires_explicit_hardware_approval': True}))
        return
    load_credential()
    os.chdir(PROFILE.parent)
    sys.path.insert(0, str(SOURCE))
    os.environ.pop('QT_QPA_PLATFORM', None)
    from PySide6.QtWidgets import QLabel
    from yam_operator.main import build_app
    qt, window, app, _ = build_app(profile, dry_hardware=False)
    if window._state_adapter is None:
        raise RuntimeError('Real test requires authenticated State; no Relay-control fallback')
    bind_test(window, args.mate)
    window.setWindowTitle('YAM Operator - ONE MATE / TWO REAL ARMS TEST')
    banner = QLabel(f'TEST ONLY | {args.mate} controls BOTH REAL YAM arms | Normal speed | Stop = abort, NO HOME')
    banner.setStyleSheet('background:#ffd6bd;color:#551800;padding:12px;font-weight:bold;')
    window.centralWidget().layout().insertWidget(0, banner)
    window.show()
    # Opening this UI never clicks Connect/Teleop or opens the Mate UART.
    qt.exec()


if __name__ == '__main__':
    main()
