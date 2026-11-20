"""Capture only synthetic packet metadata from cloud tcpdump stdout, never disk.

Cloud command is read-only, bounded to 28 seconds, non-promiscuous, UDP5004/5005 only.
All unrelated payloads are discarded in memory; saved output has no packet bytes.
"""
from collections import defaultdict
import json
import hashlib
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import uuid

from verify_relay_synthetic import main as synthetic

HERE = Path(__file__).resolve().parent


def run(video, proxy=False, pace_ms=0, warmup=0, status_rate=20, command_rate=20, lan_mirror=False, raw_load=False,
        port_comparison=False, probe_runner=None):
    prefix = 'syn_'+uuid.uuid4().hex[:20]
    events = []
    errors = []
    audit = {'pcap_records': 0, 'truncated_records': 0,
             'non_ipv4_ethernet_records': 0, 'non_udp_records': 0,
             'invalid_ip_udp_lengths': 0}
    ready = threading.Event()
    capture_command = "timeout --signal=INT --kill-after=2s 28s tcpdump -n -i eth0 -p -s 2048 -U -w - 'udp port 5005 or udp port 5004"+(' or udp port 45004' if port_comparison else '')+"'"
    proc = subprocess.Popen(['ssh', '-i', './id_rsa.pem', '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes', 'root@191.222.219.43',
           capture_command],
           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stderr():
        for line in proc.stderr:
            text = line.decode(errors='replace').strip()
            errors.append(text)
            if 'listening on' in text:
                ready.set()

    def capture():
        command_digests = {}
        def read(n):
            out = bytearray()
            while len(out) < n:
                chunk = proc.stdout.read(n-len(out))
                if not chunk: return None
                out.extend(chunk)
            return out
        header = read(24)
        if not header: return
        if header[:4] == b'\xd4\xc3\xb2\xa1': endian = '<'
        elif header[:4] == b'\xa1\xb2\xc3\xd4': endian = '>'
        else:
            errors.append('Unexpected pcap format'); return
        if struct.unpack_from(endian+'I', header, 20)[0] != 1:
            errors.append('Expected Ethernet pcap'); return
        while True:
            h = read(16)
            if not h: break
            sec, usec, size, wire_size = struct.unpack(endian+'IIII', h)
            packet = read(size)
            if packet is None: break
            audit['pcap_records'] += 1
            if size < wire_size: audit['truncated_records'] += 1
            if len(packet) < 42 or packet[12:14] != b'\x08\x00':
                audit['non_ipv4_ethernet_records'] += 1
                continue
            ip = packet[14:]
            if ip[9] != 17:
                audit['non_udp_records'] += 1
                continue
            ihl = (ip[0]&15)*4
            if ihl < 20 or len(ip) < ihl+8:
                audit['invalid_ip_udp_lengths'] += 1
                continue
            src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
            sport, dport, length, _ = struct.unpack_from('!HHHH', ip, ihl)
            if length < 8 or len(ip) < ihl+length:
                audit['invalid_ip_udp_lengths'] += 1
                continue
            payload = bytes(ip[ihl+8:ihl+length])
            payload_digest = hashlib.sha256(payload).hexdigest()[:20]
            event = {'time': sec+usec/1e6, 'src': [src,sport], 'dst': [dst,dport], 'bytes':len(payload),
                     'digest': payload_digest}
            if len(payload)>=44 and payload[:4] == b'LTRH':
                session = payload[8:44].split(b'\0')[0].decode(errors='replace')
                if not session.startswith(prefix): continue
                event['session'] = session
                event['type'] = struct.unpack_from('!H',payload,6)[0]
                payload = payload[44:]
                if payload.startswith(b'PMRC'):
                    command_digests[hashlib.sha256(payload).hexdigest()[:20]] = session
            elif payload.startswith(b'PMRC') and payload_digest in command_digests:
                event['session'] = command_digests[payload_digest]
            elif prefix.encode() not in payload:
                continue
            if payload.startswith(b'PMRC') and len(payload)==88:
                event['kind'] = 'PMRC'
                event['seq'] = struct.unpack_from('!I',payload,20)[0]
            elif payload.startswith(b'{'):
                try:
                    data = json.loads(payload)
                    if data.get('magic') != 'PMRS': continue
                    event['kind'] = 'PMRS'
                    event['seq'] = data['arms']['left']['seq']
                except (ValueError,KeyError): continue
            elif len(payload)>=12 and payload[0]>>6==2:
                event['kind'] = 'video'
                event['rtp_seq'] = struct.unpack_from('!H',payload,2)[0]
            else:
                event['kind'] = 'heartbeat'
            event['direction'] = 'cloud_in' if dst=='191.222.219.43' else 'cloud_out'
            events.append(event)

    workers = [threading.Thread(target=stderr,daemon=True),threading.Thread(target=capture,daemon=True)]
    for t in workers: t.start()
    try:
        if not ready.wait(8): raise RuntimeError('Cloud tcpdump not ready: '+repr(errors))
        print(json.dumps({'capture_ready':prefix,'video':video}),flush=True)
        if probe_runner is not None:
            probe_runner(prefix)
        else:
            try: synthetic(True,prefix=prefix,video_enabled=video,proxy_enabled=proxy,pace_ms=pace_ms,
                           warmup=warmup,status_rate=status_rate,command_rate=command_rate,lan_mirror=lan_mirror,
                           raw_load=raw_load,port_comparison=port_comparison)
            except SystemExit: pass
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=3)
        for t in workers: t.join(timeout=2)
    counts = defaultdict(list)
    for event in events:
        if 'seq' in event: counts[event['direction']+'_'+event['kind']].append(event['seq'])
    report={'prefix':prefix,'video':video,'proxy':proxy,'pace_ms':pace_ms,'warmup':warmup,
            'status_rate':status_rate,'command_rate':command_rate,'events':events,'counts':dict(counts),
            'tcpdump_stderr':errors,'capture_exit_code':proc.returncode,
            'capture_audit':audit}
    path=HERE/'.build'/('relay-capture-'+prefix+'.json')
    path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'capture_report':str(path),'counts':{k:len(v) for k,v in counts.items()},
                      'tcpdump_stderr':errors,'capture_audit':audit}),flush=True)
    return path


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',action='store_true')
    parser.add_argument('--proxy',action='store_true')
    parser.add_argument('--pace-ms',type=float,default=0)
    parser.add_argument('--warmup',type=float,default=0)
    parser.add_argument('--status-rate',type=float,default=20)
    parser.add_argument('--command-rate',type=float,default=20)
    parser.add_argument('--lan-mirror',action='store_true')
    parser.add_argument('--raw-load',action='store_true')
    parser.add_argument('--compare-ports',action='store_true')
    args=parser.parse_args()
    if args.pace_ms and not args.proxy: parser.error('--pace-ms requires --proxy')
    if args.lan_mirror and not (args.proxy or args.raw_load or args.compare_ports): parser.error('--lan-mirror requires --proxy or --raw-load')
    run(args.video or args.proxy, args.proxy, args.pace_ms,args.warmup,args.status_rate,args.command_rate,args.lan_mirror,args.raw_load or args.compare_ports,args.compare_ports)
