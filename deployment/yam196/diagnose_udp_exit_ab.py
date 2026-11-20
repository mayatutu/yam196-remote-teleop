"""Compare wired and WLAN UDP paths using per-socket interface selection only.

Keep wired192.168.1.37 for LAN access; connect WLAN to an independent hotspot.
Does not change adapter settings, routes, services, or .196. Default is offline
preflight; --run sends1000 synthetic1200-byte packets per path, about1.2MB WLAN.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

from diagnose_relay_capture import run as capture

HERE=Path(__file__).resolve().parent
PROJECT=HERE.parent.parent


def payload(prefix,role,seq):
    return (struct.pack('!IHH36s',0x4c545248,44,1,(prefix+'_'+role).encode())+
            struct.pack('!BBHII',0x80,96,seq,seq*900,1234)+bytes(1144))


def interfaces():
    command='''[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();
@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object {
 [pscustomobject]@{name=$_.InterfaceAlias;index=$_.InterfaceIndex;
 address=$_.IPv4Address.IPAddress;gateway=$_.IPv4DefaultGateway.NextHop}
}) | ConvertTo-Json -Compress'''
    data=json.loads(subprocess.check_output(['powershell','-NoProfile','-Command',command],encoding='utf-8'))
    return [data] if isinstance(data,dict) else data


def main(new_flows=False):
    os.chdir(PROJECT)
    configs=interfaces()
    wired=next((c for c in configs if c['address']=='192.168.1.37'),None)
    wlan=next((c for c in configs if c['name']=='WLAN'),None)
    if not wired or not wlan:
        raise SystemExit('Keep wired192.168.1.37 connected and connect WLAN to the phone hotspot.')
    if wired['gateway']==wlan['gateway']:
        raise SystemExit('Both adapters still use the same gateway. Connect WLAN to an independent hotspot first.')
    selected={'wired':wired,'wlan':wlan}
    sends=[]
    saved={}

    def probe(prefix):
        sockets={}
        def open_socket(cfg):
            sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP,31,struct.pack('!I',cfg['index']))
            assert sock.getsockopt(socket.IPPROTO_IP,31)==cfg['index']
            sock.bind((cfg['address'],0))
            return sock
        try:
            for role,cfg in selected.items():
                # Windows IP_UNICAST_IF=31 takes an interface index in network byte order.
                sockets[role]=open_socket(cfg)
            start=time.monotonic()
            for seq in range(2000 if new_flows else 1000):
                until=start+seq/100
                while time.monotonic()<until:
                    time.sleep(max(0,min(.003,until-time.monotonic())))
                if new_flows and seq==800:
                    for role,cfg in selected.items():sockets[role+'_new']=open_socket(cfg)
                roles=list(sockets);offset=seq%len(roles)
                for role in roles[offset:]+roles[:offset]:
                    data=payload(prefix,role,seq)
                    assert sockets[role].sendto(data,('191.222.219.43',5004))==len(data)
                    sends.append({'time':time.time(),'role':role,'seq':seq,
                                  'digest':hashlib.sha256(data).hexdigest()[:20]})
                if time.monotonic()-start>24:
                    raise TimeoutError('Bounded send duration exceeded')
        finally:
            for sock in sockets.values():sock.close()
            path=HERE/'.build'/('relay-exit-sends-'+prefix+'.json')
            path.write_text(json.dumps({'prefix':prefix,'interfaces':selected,'new_flows':new_flows,'sends':sends},indent=2)+'\n',encoding='utf-8')
            saved['prefix']=prefix

    path=capture(video=False,probe_runner=probe)
    cloud=json.loads(path.read_text(encoding='utf-8'))
    incoming=[e for e in cloud['events'] if e['direction']=='cloud_in']
    counts=Counter(e['digest'] for e in incoming)
    results={}
    for role in sorted({s['role'] for s in sends}):
        samples=[s for s in sends if s['role']==role]
        received=0
        for item in samples:
            if counts[item['digest']]:received+=1;counts[item['digest']]-=1
        ips=sorted({e['src'][0] for e in incoming if e.get('session')==saved['prefix']+'_'+role})
        results[role]={'sent':len(samples),'received':received,'missing':len(samples)-received,'source_public_ips':ips}
    distinct=(bool(results['wired']['source_public_ips']) and bool(results['wlan']['source_public_ips']) and
              set(results['wired']['source_public_ips']).isdisjoint(results['wlan']['source_public_ips']))
    report={'prefix':saved['prefix'],'results':results,'new_flows':new_flows,'independent_public_sources_confirmed':distinct,
            'tcpdump_messages':cloud['tcpdump_stderr']}
    out=HERE/'.build'/('relay-exit-ab-'+saved['prefix']+'.json')
    out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)
    print('Saved: '+str(out),flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--new-flows',action='store_true')
    args=parser.parse_args()
    data=payload('syn_0123456789abcdef0123','wired',999)
    assert len(data)==1200 and data[8:44].split(b'\0')[0]==b'syn_0123456789abcdef0123_wired'
    print('Offline payload preflight passed; no routes or adapter settings changed.')
    if args.run:main(args.new_flows)
