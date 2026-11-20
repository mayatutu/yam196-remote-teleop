"""Bounded packet-size/rate/return-path comparison; offline unless --run.

Uses the existing Relay and the wired Windows interface. No remote files,
listeners, services or routing changes. Duplex registers its own synthetic
session receiver on the same socket; Relay returns only that session's payload.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import struct
import time

from diagnose_relay_capture import run as capture
from diagnose_udp_exit_ab import interfaces

HERE = Path(__file__).resolve().parent
DEST = ('191.222.219.43', 5004)
CASES = {'s10': (128, 10), 's100': (128, 100),
         'l10': (1200, 10), 'l100': (1200, 100),
         'duplex': (1200, 100)}


def header(prefix, role, packet_type=1):
    session = (prefix+'_'+role).encode()
    assert len(session) <= 36
    return struct.pack('!IHH36s', 0x4c545248, 44, packet_type, session)


def packet(prefix, role, seq, size):
    body = (struct.pack('!BBHII', 0x80, 96, seq, seq*900, 1234)
            + (prefix+'_'+role).encode())
    return header(prefix, role)+body+bytes(size-44-len(body))


def digest(data):
    return hashlib.sha256(data).hexdigest()[:20]


def main():
    os.chdir(HERE.parent.parent)
    cfg = next((c for c in interfaces() if c['address']=='192.168.1.37'), None)
    if cfg is None:
        raise SystemExit('Current wired Windows interface 192.168.1.37 required.')
    saved = {}

    def probe(prefix):
        sockets, sends, replies = {}, [], []
        try:
            for role in CASES:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack('!I', cfg['index']))
                assert sock.getsockopt(socket.IPPROTO_IP, 31) == cfg['index']
                sock.bind((cfg['address'], 0))
                sock.setblocking(False)
                sockets[role] = sock
            start = time.monotonic()
            def send(role, data, seq, kind):
                assert sockets[role].sendto(data, DEST) == len(data)
                sends.append({'time': time.time(), 'role': role, 'seq': seq,
                              'kind': kind, 'bytes': len(data), 'digest': digest(data),
                              'body_digest': digest(data[44:])})
            for tick in range(2000):
                target = start+tick/100
                while time.monotonic() < target:
                    time.sleep(max(0, min(.002, target-time.monotonic())))
                if tick % 100 == 0:
                    send('duplex', header(prefix, 'duplex', 2), tick, 'heartbeat')
                roles = list(CASES)
                offset = tick % len(roles)
                for role in roles[offset:]+roles[:offset]:
                    size, rate = CASES[role]
                    if tick % (100//rate) == 0:
                        send(role, packet(prefix, role, tick, size), tick, 'data')
                for sock in select.select(list(sockets.values()), [], [], 0)[0]:
                    for _ in range(32):
                        try: data, addr = sock.recvfrom(2048)
                        except BlockingIOError: break
                        if addr == DEST and prefix.encode() in data:
                            replies.append({'time': time.time(), 'digest': digest(data)})
                if time.monotonic()-start > 23:
                    raise TimeoutError('Bounded send duration exceeded')
            until = time.monotonic()+.5
            while time.monotonic() < until:
                for sock in select.select(list(sockets.values()), [], [], .02)[0]:
                    data, addr = sock.recvfrom(2048)
                    if addr == DEST and prefix.encode() in data:
                        replies.append({'time': time.time(), 'digest': digest(data)})
        finally:
            for sock in sockets.values(): sock.close()
            saved.update(prefix=prefix, interface=cfg, sends=sends, replies=replies)
            out = HERE/'.build'/('relay-shape-sends-'+prefix+'.json')
            out.write_text(json.dumps(saved, indent=2)+'\n', encoding='utf-8')

    path = capture(False, probe_runner=probe)
    cloud = json.loads(path.read_text(encoding='utf-8'))
    incoming = Counter(e['digest'] for e in cloud['events'] if e['direction']=='cloud_in')
    outgoing = Counter(e['digest'] for e in cloud['events'] if e['direction']=='cloud_out')
    local = Counter(e['digest'] for e in saved['replies'])
    results = {}
    for role, (size, rate) in CASES.items():
        rows = [s for s in saved['sends'] if s['role']==role and s['kind']=='data']
        missing = [s for s in rows if not incoming[s['digest']]]
        # Each data body and full packet is unique within the test.
        assert len({s['digest'] for s in rows}) == len(rows)
        results[role] = {'bytes': size, 'pps': rate, 'sent': len(rows),
                         'cloud_in': len(rows)-len(missing), 'missing': len(missing),
                         'cloud_out': sum(bool(outgoing[s['body_digest']]) for s in rows),
                         'windows_replies': sum(bool(local[s['body_digest']]) for s in rows),
                         'missing_by_nominal_second': {
                             str(sec): sum(s['seq']//100==sec for s in missing)
                             for sec in range(20)}}
    report = {'prefix': saved['prefix'], 'results': results,
              'capture_audit': cloud['capture_audit'],
              'tcpdump_stderr': cloud['tcpdump_stderr'],
              'interface': cfg, 'capture_report': str(path)}
    out = HERE/'.build'/('relay-shape-'+saved['prefix']+'.json')
    out.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    for role, (size, _) in CASES.items():
        p = packet('syn_0123456789abcdef0123', role, 1999, size)
        assert len(p)==size and p[:4]==b'LTRH'
        assert b'syn_0123456789abcdef0123' in p[44:]
    print('Offline shape preflight passed; no sockets or capture started.')
    if args.run: main()
