"""One interactive command: sudo egress observation + cloud/LAN synthetic test.

Default is an offline parser/preflight check. --run must run in the user's own
terminal. SSH/sudo handles authentication; this program never reads a password.
Only synthetic UDP packet metadata is retained locally. No remote files or
configuration changes, and no real devices or services are started/stopped.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading
import time

from diagnose_relay_capture import run as cloud_test

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
FILTER = ('udp and dst host 191.222.219.43 and '
          '(dst port 5004 or dst port 5005) and '
          'udp[8:4] = 0x4c545248 and udp[16:4] = 0x73796e5f')
REMOTE = ('sudo -v && sudo -n /usr/bin/timeout --signal=INT --kill-after=2s 36s '
          '/usr/bin/tcpdump -n -i wlp132s0f0 -Q out -p -s 2048 -l -tt -xx '
          "'"+FILTER+"'")


def capture_is_ready(text):
    # tcpdump's default verbosity prints an unprefixed "listening on ...";
    # other builds prefix this line with "tcpdump:".
    return re.search(r'(?:^|\n)(?:tcpdump:\s*)?listening on wlp132s0f0\b', text) is not None


class PacketReader:
    def __init__(self):
        self.packet = bytearray()
        self.events = []
        self.messages = []
        self.incomplete = 0

    def flush(self):
        packet = bytes(self.packet)
        self.packet.clear()
        if len(packet) < 42 or packet[12:14] != b'\x08\x00':
            return
        ip = packet[14:]
        ihl = (ip[0]&15)*4
        if ip[9] != 17 or len(ip)<ihl+8:
            return
        ulen = struct.unpack_from('!H', ip, ihl+4)[0]
        payload = ip[ihl+8:ihl+ulen]
        if len(payload) != ulen-8:
            self.incomplete += 1
            return
        if len(payload)<44 or payload[:4] != b'LTRH':
            return
        session = payload[8:44].split(b'\0')[0].decode(errors='replace')
        if not session.startswith('syn_'):
            return
        self.events.append({'session':session,'bytes':len(payload),
                            'digest':hashlib.sha256(payload).hexdigest()[:20]})

    def line(self, line):
        if re.match(r'^\s*0x[0-9a-fA-F]+:', line):
            tokens = line.split(':',1)[1].split()
            for token in tokens:
                if not re.fullmatch(r'(?:[0-9a-fA-F]{4}|[0-9a-fA-F]{2})',token):
                    break
                self.packet.extend(bytes.fromhex(token))
        else:
            self.flush()
            if 'tcpdump:' in line or 'packets ' in line:
                self.messages.append(line.strip())


def match_sends(sends, events):
    available = Counter(x['digest'] for x in events)
    missing = []
    for item in sends:
        if available[item['digest']]:
            available[item['digest']] -= 1
        else:
            missing.append(item)
    return {'matched':len(sends)-len(missing),'missing':len(missing),
            'missing_by_role':dict(Counter(x['role'] for x in missing))}


def preflight():
    assert capture_is_ready('tcpdump: verbose output suppressed\nlistening on wlp132s0f0, link-type EN10MB\n')
    assert capture_is_ready('tcpdump: listening on wlp132s0f0, link-type EN10MB\n')
    assert not capture_is_ready('sudo: a password is required\n')
    # Exercise Ethernet/IP/UDP extraction from the real tcpdump -xx text format,
    # including its short final hex group and rejection of truncated packets.
    prefix = b'syn_0123456789abcdef0123_head'
    payload = struct.pack('!IHH36s',0x4c545248,44,1,prefix)+b'odd'
    udp = struct.pack('!HHHH',12345,5004,len(payload)+8,0)+payload
    ip = bytearray(20);ip[0]=0x45;ip[9]=17
    packet = bytes(12)+b'\x08\x00'+ip+udp
    parser = PacketReader()
    parser.line('12345.678 IP 192.168.1.196.12345 > 191.222.219.43.5004: UDP')
    for offset in range(0,len(packet),16):
        chunk = packet[offset:offset+16]
        parser.line('  0x%04x:  '%offset+' '.join(chunk[i:i+2].hex() for i in range(0,len(chunk),2)))
    parser.flush()
    assert len(parser.events)==1
    assert parser.events[0]['digest']==hashlib.sha256(payload).hexdigest()[:20]
    parser.packet.extend(packet[:-1]);parser.flush();assert parser.incomplete==1
    assert match_sends([{'digest':'a','role':'PMRS'}],[{'digest':'a'}])['missing']==0
    assert match_sends([{'digest':'a','role':'PMRS'}],[])['missing']==1
    print('Offline preflight passed; no connections or capture started.')


def main():
    if not sys.stdin.isatty():
        raise SystemExit('Run --run in your own interactive terminal so sudo can prompt securely.')
    os.chdir(PROJECT)
    probe = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        probe.connect(('192.168.1.196',9))
        if probe.getsockname()[0] != '192.168.1.37':
            raise RuntimeError('Windows LAN address changed; diagnostic mirror needs updating')
    finally:
        probe.close()
    print('SSH/sudo may ask for the .196 system password in this terminal.\n'
          'After authentication, the synthetic tests run automatically.\n'
          'No remote files, configuration changes or real devices.',flush=True)
    proc = subprocess.Popen(['ssh','-tt','-o','BatchMode=yes','-o','ConnectTimeout=10',
            '-o','StrictHostKeyChecking=yes','yam196',REMOTE],
            stdin=None,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    reader = PacketReader()
    ready = threading.Event()

    def consume():
        pending = ''
        authenticated = False
        while True:
            chunk = os.read(proc.stdout.fileno(),65536)
            if not chunk: break
            text = chunk.decode('ascii',errors='replace').replace('\r','')
            if not authenticated:
                # Forward sudo's terminal prompt, never record it in evidence.
                sys.stderr.write(text);sys.stderr.flush()
            pending += text
            if capture_is_ready(pending):
                authenticated = True
                ready.set()
            while '\n' in pending:
                line,pending = pending.split('\n',1)
                reader.line(line)
        if pending: reader.line(pending)
        reader.flush()

    worker = threading.Thread(target=consume,daemon=True)
    worker.start()
    capture_path = None
    try:
        until = time.monotonic()+180
        while not ready.wait(.25):
            if proc.poll() is not None:
                raise RuntimeError('Capture readiness was not observed before SSH exited; review the terminal output above')
            if time.monotonic()>until:
                raise TimeoutError('Authentication timeout')
        print('\n.196 egress capture ready. Starting cloud/LAN comparison.',flush=True)
        capture_path = cloud_test(video=True,proxy=True,lan_mirror=True)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=3)
        worker.join(timeout=3)
    cloud = json.loads(capture_path.read_text(encoding='utf-8'))
    prefix = cloud['prefix']
    endpoint = json.loads((HERE/'.build'/('relay-synthetic-'+prefix+'.json')).read_text(encoding='utf-8'))
    sends = endpoint['remote']['udp_sends']
    nic_events = [x for x in reader.events if x['session'].startswith(prefix+'_')]
    ingress_events = [x for x in cloud['events'] if x['direction']=='cloud_in']
    nic = match_sends(sends,nic_events)
    ingress = match_sends(sends,ingress_events)
    lan = match_sends(sends,endpoint['lan_events'])
    notes = '\n'.join(reader.messages)
    complete = ('0 packets dropped by kernel' in notes and reader.incomplete==0 and
                proc.returncode in (0,124,130))
    if not complete:
        result = 'Egress observation incomplete; do not assign a drop location from this capture.'
    elif not nic['missing'] and ingress['missing']:
        result = 'Packets reached .196 egress but not cloud NIC: investigate gateway/WAN/cloud pre-NIC path.'
    elif nic['missing']:
        result = 'Some OS-accepted sends absent at .196 egress: investigate host public-route/filter path first.'
    else:
        result = 'No upstream packet loss reproduced in this run; repeat before drawing a conclusion.'
    report = {'prefix':prefix,'os_sends':len(sends),'nic':nic,'cloud':ingress,'lan':lan,
              'egress_observation_complete':complete,'nic_events':nic_events,
              'tcpdump_messages':reader.messages,'incomplete_packets':reader.incomplete,
              'capture_exit_code':proc.returncode,'result':result}
    out = HERE/'.build'/('relay-egress-'+prefix+'.json')
    out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['os_sends','nic','cloud','lan','result']},indent=2),flush=True)
    print('Saved: '+str(out),flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    preflight()
    if args.run:main()
