"""Authorized Windows <-> Relay <-> .196 synthetic test. No remote file writes.

Run explicitly with --run; default performs local preflight only. Remote program
is interpreted from SSH stdin with bytecode disabled and a 32-second watchdog.
"""
import argparse
import ast
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
SDK = Path('E:/working/YAM/YAM/piper-mate/Python_SDK')
sys.path.insert(0, str(SDK))
from relay_bridge_proto import encode_relay_header
from remote_dual_protocol import ArmBlockV3, DualCommandV3, decode_status, encode_command


def main(run, *, prefix=None, video_enabled=True, proxy_enabled=False, pace_ms=0,
         warmup=0, status_rate=20, command_rate=20, lan_mirror=False, raw_load=False,
         port_comparison=False):
    source = (HERE / 'relay_synthetic_endpoint.py').read_text(encoding='utf-8')
    ast.parse(source)
    assert 'syn_' in source and "'fake', 'libx265'" in source
    assert '--health-file' not in source
    assert 'yam_remote_dual_server' not in source
    if not run:
        print('Local preflight passed; no network traffic or child processes started.')
        return
    prefix = prefix or 'syn_'+uuid.uuid4().hex[:20]
    if not re.fullmatch(r'syn_[0-9a-f]{20}', prefix):
        raise ValueError('Only random synthetic prefixes are allowed')
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'prefix': prefix,
              'endpoint_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'remote_files_written': False, 'video_decode_tested': False,
              'video_enabled': video_enabled,
              'proxy_enabled': proxy_enabled,
              'pace_ms': pace_ms,
              'warmup': warmup, 'status_rate': status_rate, 'command_rate': command_rate,
              'lan_mirror': lan_mirror, 'lan_events': [],
              'raw_load': raw_load,
              'port_comparison': port_comparison,
              'commands_sent': 0, 'statuses_received': 0, 'status_sequences': [],
              'errors': [], 'rtt_ms': [], 'isolation_packets': 0, 'video': {}}
    messages = queue.Queue()
    remote_err, client_log = deque(maxlen=100), deque(maxlen=100)
    sockets, procs, threads = [], [], []
    remote_report = None

    def lines(stream, dest, parse=False):
        for line in stream:
            if parse:
                try: dest.put(json.loads(line))
                except ValueError: remote_err.append(line.rstrip())
            else: dest.append(line.rstrip())

    def thread(stream, dest, parse=False):
        worker = threading.Thread(target=lines, args=(stream, dest, parse), daemon=True)
        worker.start(); threads.append(worker)

    def udp(ip='0.0.0.0'):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((ip, 0)); sock.setblocking(False); sockets.append(sock)
        return sock

    try:
        command = udp('127.0.0.1')
        reserve = udp('127.0.0.1'); client_port = reserve.getsockname()[1]
        reserve.close(); sockets.remove(reserve)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        client = subprocess.Popen([sys.executable, '-B', '-u', str(SDK/'arm_relay_client_bridge.py'),
                  '--relay-ip', '191.222.219.43', '--relay-port', '5005', '--session-id', prefix+'_arm',
                  '--local-bind-ip', '127.0.0.1', '--local-port', str(client_port),
                  '--dual-client-ip', '127.0.0.1'], stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True, env=env)
        procs.append(client); thread(client.stdout, client_log)
        videos = {udp(): role for role in (('head', 'left', 'right') if video_enabled else ())}
        isolation = udp()
        mirror_socket = udp() if lan_mirror else None
        mirror_destination = '192.168.1.37:'+str(mirror_socket.getsockname()[1]) if lan_mirror else '-'
        for role in videos.values():
            report['video'][role] = {'rtp_packets': 0, 'rtp_timestamps': set(),
                                    'marker_timestamps': set(), 'stats_packets': 0,
                                    'network_acks_sent': 0, 'unexpected_packets': 0}
        for sock, role in videos.items():
            sock.sendto(encode_relay_header(2, prefix+'_'+role), ('191.222.219.43', 5004))
        remote = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                  '-o', 'StrictHostKeyChecking=yes', 'yam196',
                  'timeout', '--signal=TERM', '--kill-after=3s', '32s', 'env',
                  'PYTHONDONTWRITEBYTECODE=1', 'python3', '-B', '-u', '-', prefix,
                  'raw-load' if raw_load else ('proxy-video' if proxy_enabled else ('video' if video_enabled else 'no-video')),
                  str(pace_ms), str(warmup), str(status_rate), mirror_destination,
                  'compare-ports' if port_comparison else '-'],
                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, encoding='utf-8')
        procs.append(remote); thread(remote.stdout, messages, True); thread(remote.stderr, remote_err)
        remote.stdin.write(source); remote.stdin.close()
        ready_deadline = time.monotonic()+12
        while True:
            if remote.poll() is not None:
                raise RuntimeError('Remote endpoint exited before readiness')
            if time.monotonic() > ready_deadline:
                raise TimeoutError('Remote readiness timeout')
            try: message = messages.get(timeout=0.1)
            except queue.Empty: continue
            if message.get('event') == 'ready':
                report['remote_ready'] = message
                print(json.dumps(message), flush=True)
                break
        start = time.monotonic()
        next_send, next_hb, seq = start+warmup, start, 0
        sent_at, statuses = {}, set()
        while time.monotonic()-start < 19+warmup:
            now = time.monotonic()
            if client.poll() is not None:
                raise RuntimeError('Windows bridge exited unexpectedly')
            if now >= next_hb and now-start < 15+warmup:
                for sock, role in videos.items():
                    sock.sendto(encode_relay_header(2, prefix+'_'+role), ('191.222.219.43', 5004))
                isolation.sendto(encode_relay_header(2, prefix+'_iso'), ('191.222.219.43', 5004))
                next_hb = now+0.5
            if now >= next_send and seq < 240:
                seq += 1
                cmd = DualCommandV3(time.time_ns(), ArmBlockV3(seq, (seq,0,0,0,0,0)),
                                    ArmBlockV3(seq, (0,seq,0,0,0,0)))
                command.sendto(encode_command(cmd), ('127.0.0.1', client_port))
                sent_at[seq] = now; report['commands_sent'] += 1
                # Absolute schedule avoids cumulative Windows timer rounding;
                # all 240 packets finish around 12s, leaving ~3s to drain replies.
                next_send += 1/command_rate
            readable, _, _ = select.select(sockets, [], [], 0.01)
            for sock in readable:
                payload, src = sock.recvfrom(65535)
                if sock is mirror_socket:
                    assert src[0] == '192.168.1.196'
                    assert payload[8:44].split(b'\0')[0].decode().startswith(prefix)
                    report['lan_events'].append({'time': time.time(),
                         'digest':hashlib.sha256(payload).hexdigest()[:20],'bytes':len(payload)})
                elif sock is command:
                    status = decode_status(payload)
                    left, right = status.arms['left'], status.arms['right']
                    assert not left.enabled and not right.enabled and left.safe_state and right.safe_state
                    assert left.seq == right.seq and left.seq in sent_at
                    statuses.add(left.seq); report['statuses_received'] += 1
                    report['rtt_ms'].append(round((time.monotonic()-sent_at[left.seq])*1000, 2))
                elif sock is isolation:
                    report['isolation_packets'] += 1
                else:
                    role = videos[sock]; data = report['video'][role]
                    if src != ('191.222.219.43', 5004):
                        data['unexpected_packets'] += 1
                    elif len(payload) >= 44 and struct.unpack_from('!IHH', payload) == (0x4c545248,44,3):
                        assert payload[8:44].split(b'\0')[0].decode() == prefix+'_'+role
                        data['stats_packets'] += 1
                    elif len(payload) >= 12 and payload[0] >> 6 == 2:
                        timestamp = struct.unpack_from('!I', payload, 4)[0]
                        data['rtp_packets'] += 1; data['rtp_timestamps'].add(timestamp)
                        if payload[1] & 0x80 and timestamp not in data['marker_timestamps']:
                            data['marker_timestamps'].add(timestamp)
                            ack = struct.pack('<III', 0x4641434b, 1, timestamp)
                            sock.sendto(encode_relay_header(2, prefix+'_'+role)+ack, src)
                            data['network_acks_sent'] += 1
                    else:
                        data['unexpected_packets'] += 1
            try:
                while True:
                    message = messages.get_nowait()
                    if message.get('event') == 'result': remote_report = message['report']
            except queue.Empty: pass
            if remote_report is not None: break
        report['status_sequences'] = sorted(statuses)
        if remote_report is None: raise TimeoutError('Remote result timeout')
        remote.wait(timeout=3)
        report['remote'] = remote_report
        expected = sum(x['role']=='PMRS' for x in remote_report['udp_sends'])
        report['statuses_sent_remote'] = expected
        report['command_delivery_fraction'] = remote_report['commands_received']/max(1,report['commands_sent'])
        report['status_delivery_fraction'] = len(statuses)/max(1,expected)
        assert remote.returncode == 0, 'Remote endpoint failed'
        assert remote_report['protected_state_unchanged'], 'Protected state changed'
        assert not remote_report['command_errors'], 'Malformed commands at remote endpoint'
        assert report['commands_sent'] >= 200, 'Insufficient simulated command samples'
        assert report['command_delivery_fraction'] >= 0.8, 'Fewer than 80% PMRC commands received'
        assert report['isolation_packets'] == 0, 'Unexpected traffic in independent session'
        for role, data in report['video'].items():
            stats = [line for line in remote_report['logs'][role] if line.startswith('[stats]')]
            data['sender_encoded_total'] = max([int(re.search(r'encoded_total=(\d+)', l)[1]) for l in stats], default=0)
            data['sender_network_acks'] = sum(int(re.search(r'net_acked=(\d+)', l)[1]) for l in stats)
            data['sender_ingress_acks'] = sum(int(re.search(r'relay_ingress_ack=(\d+)', l)[1]) for l in stats)
            assert data['rtp_packets'] >= 100 and len(data['rtp_timestamps']) >= 100, role+' insufficient RTP'
            assert data['sender_encoded_total'] >= 100, role+' insufficient encoding'
            assert data['sender_network_acks'] > 0, role+' missing network ACK'
            assert data['unexpected_packets'] == 0, role+' unexpected packets'
        assert expected > 0 and len(statuses) >= expected * 0.8, 'Fewer than 80% sent PMRS responses'
        report['probe_checks_passed'] = True
        report['control_packets_complete'] = (remote_report['commands_received']==report['commands_sent']
                                             and len(statuses)==expected)
    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                # Remote watchdog independently owns cleanup if SSH is interrupted.
                proc.terminate()
            try: proc.wait(timeout=4)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
        for sock in sockets: sock.close()
        for worker in threads: worker.join(timeout=1)
        report['local_children_exited'] = [p.returncode for p in procs]
        report['client_log'] = list(client_log); report['remote_stderr'] = list(remote_err)
        for data in report['video'].values():
            for key in ('rtp_timestamps', 'marker_timestamps'):
                data[key] = sorted(data[key])
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        out = HERE/'.build'/('relay-synthetic-'+prefix+'.json')
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'report': str(out),'checks_passed':report.get('probe_checks_passed',False),
                          'control_packets_complete':report.get('control_packets_complete',False),
                          'commands_sent':report['commands_sent'],
                          'commands_received':report.get('remote',{}).get('commands_received'),
                          'statuses_sent':report.get('statuses_sent_remote'),
                          'statuses_received':report['statuses_received'],'errors':report['errors']}), flush=True)
    if report['errors']: raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--no-video', action='store_true')
    args = parser.parse_args()
    main(args.run, video_enabled=not args.no_video)
