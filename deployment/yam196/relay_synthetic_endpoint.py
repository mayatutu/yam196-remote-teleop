"""Bounded, stdin-only .196 endpoint; no service, device or file writes."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = Path('/home/ai/yam-teleop')
RELEASE = ROOT / 'current'
SDK = RELEASE / 'piper-mate/Python_SDK'
sys.path.insert(0, str(SDK))
from relay_bridge_proto import encode_relay_header
from arm_relay_server_bridge import extract_pmrc_from_relay
from remote_dual_protocol import ArmStatus, DualStatusPacket, decode_command, encode_status

UNITS = ['yam2-gui.service', 'yam2-web.service', 'yam2-policy.service',
         'yam-teleop-agent.service', 'yam-teleop-video@head.service',
         'yam-teleop-video@left.service', 'yam-teleop-video@right.service', 'yam-teleop.target']


def snapshot():
    result = subprocess.run(['systemctl', '--user', 'show', *UNITS,
                             '--property=Id,ActiveState,SubState,UnitFileState,MainPID'],
                            capture_output=True, text=True, check=True, timeout=5)
    paths = [ROOT / 'config/station.json', ROOT / 'runtime/mode.json']
    return {'services': result.stdout,
            'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in paths if p.is_file()}}


def main():
    prefix = sys.argv[1]
    video_enabled = len(sys.argv) < 3 or sys.argv[2] in ('video', 'proxy-video')
    proxy_enabled = len(sys.argv) >= 3 and sys.argv[2] == 'proxy-video'
    raw_load = len(sys.argv) >= 3 and sys.argv[2] == 'raw-load'
    port_comparison = len(sys.argv) >= 8 and sys.argv[7] == 'compare-ports'
    pace_ms = float(sys.argv[3]) if len(sys.argv) >= 4 else 0.0
    warmup = float(sys.argv[4]) if len(sys.argv) >= 5 else 0.0
    status_rate = float(sys.argv[5]) if len(sys.argv) >= 6 else 20.0
    mirror_address = None
    if len(sys.argv) >= 7 and sys.argv[6] != '-':
        host, port = sys.argv[6].split(':')
        assert host == '192.168.1.37', 'Only current diagnostic Windows LAN address allowed'
        mirror_address = (host, int(port))
    assert 0 <= pace_ms <= 5
    assert 0 <= warmup <= 5 and 0 < status_rate <= 60
    if not prefix.startswith('syn_') or len(prefix) != 24 or not prefix[4:].isalnum():
        raise ValueError('Expected independent synthetic session prefix')
    before = snapshot()
    for block in before['services'].split('\n\n'):
        if 'Id=yam-teleop' in block:
            assert 'ActiveState=inactive' in block and 'UnitFileState=disabled' in block
    processes, logs, readers = {}, {}, []
    sockets = []

    def start(name, command):
        log = logs[name] = deque(maxlen=220)
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        processes[name] = proc
        def drain():
            for line in proc.stdout:
                log.append(line.rstrip())
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        readers.append(thread)

    def stop(_signum, _frame):
        raise InterruptedError('Endpoint deadline or interruption')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    report = {'prefix': prefix, 'before': before, 'commands_received': 0,
              'command_sequences': [], 'command_errors': [], 'ingress_probe_sent': 0,
              'ingress_ack_received': 0, 'ingress_other_received': 0, 'udp_sends': [],
              'pace_ms': pace_ms, 'warmup': warmup, 'status_rate': status_rate}
    proxies, native_targets = {}, {}
    raw_sockets = {}
    mirror = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if mirror_address else None
    if mirror: sockets.append(mirror)
    last_video_send = 0
    def send_observed(sock, payload, address, role, seq=None):
        nonlocal last_video_send
        if role != 'PMRS' and pace_ms:
            remaining = last_video_send+pace_ms/1000-time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            last_video_send = time.monotonic()
        count = sock.sendto(payload, address)
        assert count == len(payload), 'Short UDP send'
        report['udp_sends'].append({'time': time.time(), 'role': role, 'seq': seq,
                                   'bytes': count, 'digest': hashlib.sha256(payload).hexdigest()[:20]})
        if mirror:
            assert mirror.sendto(payload, mirror_address) == count
    try:
        if raw_load:
            for port in ((5004, 5005, 45004) if port_comparison else (5004, 5005)):
                raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                raw.bind(('0.0.0.0', 0)); sockets.append(raw); raw_sockets[port] = raw
        # Protocol-only fake, never invokes the installed bridge's guarded main.
        fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fake.bind(('0.0.0.0', 0)); fake.setblocking(False); sockets.append(fake)
        arm_relay = ('191.222.219.43', 5005)
        fake.sendto(encode_relay_header(2, prefix+'_arm'), arm_relay)
        report['installed_server_bridge_main_tested'] = False
        for role in (('head', 'left', 'right') if video_enabled else ()):
            host, port = '191.222.219.43', 5004
            if proxy_enabled:
                proxy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                proxy.bind(('0.0.0.0', 0)); proxy.setblocking(False)
                sockets.append(proxy); proxies[proxy] = role
                host, port = '127.0.0.1', proxy.getsockname()[1]
            start(role, [str(RELEASE / 'low-latency-teleop/sender'), '--mode', 'relay',
                  '--relay-ip', host, '--relay-port', str(port), '--session', prefix+'_'+role,
                  '--width', '160', '--height', '120', '--fps', '10', '--bitrate', '100000',
                  'fake', 'libx265'])
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(('0.0.0.0', 0)); probe.setblocking(False); sockets.append(probe)
        print(json.dumps({'event': 'ready', 'prefix': prefix, 'pids':
                          {k: p.pid for k, p in processes.items()}}), flush=True)
        deadline, next_probe, next_heartbeat = time.monotonic()+15+warmup, 0, 0
        last_status = 0
        next_load, raw_seq = time.monotonic(), 0
        seen = set()
        while time.monotonic() < deadline:
            if raw_load and raw_seq < 1000 and time.monotonic() >= next_load:
                ports = list(raw_sockets)
                offset = raw_seq % len(ports)
                # Rotate order so a microburst-position effect cannot mimic a port filter.
                for port in ports[offset:]+ports[:offset]:
                    raw = raw_sockets[port]
                    role = 'load'+str(port)
                    payload = (encode_relay_header(1, prefix+'_'+role)+
                               struct.pack('!BBHII',0x80,0x60,raw_seq,raw_seq*900,1234)+bytes(1144))
                    send_observed(raw,payload,('191.222.219.43',port),role,raw_seq)
                raw_seq += 1
                next_load += 0.01
            if time.monotonic() >= next_heartbeat:
                fake.sendto(encode_relay_header(2, prefix+'_arm'), arm_relay)
                next_heartbeat = time.monotonic()+0.5
            if time.monotonic() >= next_probe:
                seq = report['ingress_probe_sent']
                payload = struct.pack('!BBHII', 0x80, 0xe0, seq, seq*9000, 1234)+b'\x26\x01\x80'
                probe.sendto(encode_relay_header(1, prefix+'_ing')+payload, ('191.222.219.43', 5004))
                report['ingress_probe_sent'] += 1
                next_probe = time.monotonic()+0.1
            poll_timeout = min(0.03,max(0,next_load-time.monotonic())) if raw_load and raw_seq<1000 else 0.03
            readable, _, _ = select.select([s for s in sockets if s is not mirror and s not in raw_sockets.values()], [], [], poll_timeout)
            for sock in readable:
                payload, source = sock.recvfrom(65535)
                if sock in proxies:
                    role = proxies[sock]
                    if source == ('191.222.219.43', 5004):
                        if sock in native_targets:
                            sock.sendto(payload, native_targets[sock])
                    elif source[0] == '127.0.0.1':
                        assert payload[8:44].split(b'\0')[0].decode() == prefix+'_'+role
                        native_targets[sock] = source
                        send_observed(sock, payload, ('191.222.219.43', 5004), role)
                    continue
                if sock is probe:
                    if (source == ('191.222.219.43', 5004) and len(payload) >= 44 and
                        struct.unpack_from('!IHH', payload) == (0x4c545248, 44, 4) and
                        payload[8:44].split(b'\0')[0].decode() == prefix+'_ing'):
                        report['ingress_ack_received'] += 1
                    else:
                        report['ingress_other_received'] += 1
                    continue
                try:
                    assert source == arm_relay, 'Unexpected arm source address'
                    payload = extract_pmrc_from_relay(payload, prefix+'_arm')
                    cmd = decode_command(payload)
                    seq = cmd.left.seq
                    assert cmd.right.seq == seq and 1 <= seq <= 240
                    assert cmd.left.joint_targets == (seq, 0, 0, 0, 0, 0)
                    assert cmd.right.joint_targets == (0, seq, 0, 0, 0, 0)
                    seen.add(seq)
                    report['commands_received'] += 1
                    arm = ArmStatus(False, seq, len(seen), 0, 0, 0, 0, 0, True, 0,
                                    prefix+' synthetic endpoint; no hardware')
                    status = encode_status(DualStatusPacket({'left': arm, 'right': arm}))
                    if time.monotonic()-last_status >= 1/status_rate-0.005:
                        send_observed(fake, encode_relay_header(2, prefix+'_arm')+status,
                                      arm_relay, 'PMRS', seq)
                        last_status = time.monotonic()
                except Exception as exc:
                    report['command_errors'].append(str(exc))
        report['command_sequences'] = sorted(seen)
    finally:
        for proc in reversed(list(processes.values())):
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in processes.values():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=3)
        for thread in readers:
            thread.join(timeout=1)
        for sock in sockets:
            sock.close()
        report['children_exited'] = {k: p.returncode for k, p in processes.items()}
        report['logs'] = {k: list(v) for k, v in logs.items()}
        report['after'] = snapshot()
        report['protected_state_unchanged'] = report['before'] == report['after']
        print(json.dumps({'event': 'result', 'report': report}), flush=True)


if __name__ == '__main__':
    main()
