"""Install an approved, hash-checked bundle. Never starts/stops/enables a service.

The only systemd operation is user daemon-reload after installing NEW units.
No apt install, sudo, CAN commands, camera enumeration, or public-network checks.
All Python package installation uses the existing uv cache plus supplied files offline.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path('/home/ai/yam-teleop')
SOURCE = Path(__file__).resolve().parent
UNIT_DIR = Path('/home/ai/.config/systemd/user')


def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()


def run(*cmd, cwd=None, env=None):
    print('RUN', ' '.join(str(x) for x in cmd), flush=True)
    subprocess.run([str(x) for x in cmd], cwd=cwd, env=env, check=True)


def verify_files(base, files):
    for relative, expected in files.items():
        p = (base / relative).resolve()
        if not p.is_relative_to(base.resolve()) or not p.is_file() or sha(p) != expected:
            raise RuntimeError(f'Baseline/checksum mismatch: {relative}')


def protected_check(baseline):
    for path, expected in baseline['protected'].items():
        if sha(Path(path)) != expected:
            raise RuntimeError(f'Existing evaluation/driver configuration changed: {path}')
    verify_files(Path('/home/ai/i2rt'), baseline['i2rt_files'])


def safe_extract(archive, target):
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            dest = (target / member.name).resolve()
            if not dest.is_relative_to(target.resolve()) or member.isdev():
                raise RuntimeError(f'Unsafe archive member: {member.name}')
            if member.issym() or member.islnk():
                link = (dest.parent / member.linkname) if member.issym() else (target / member.linkname)
                if not link.resolve().is_relative_to(target.resolve()):
                    raise RuntimeError(f'Unsafe archive link: {member.name}')
        tar.extractall(target)


def units():
    shared = ('[Service]\nType=simple\nRestart=no\nKillMode=control-group\n'
              'TimeoutStopSec=30\nUMask=0077\n'
              'Environment=PYTHONDONTWRITEBYTECODE=1\n'
              'Environment=PYTHONUNBUFFERED=1\n'
              'Environment=PYTHONPATH=/home/ai/yam-teleop/current/python:/home/ai/yam-teleop/current/arena-client/yam_client:/home/ai/yam-teleop/current/piper-mate/Python_SDK\n'
              'WorkingDirectory=/home/ai/yam-teleop/runtime/arena-client\n')
    prefix = '/home/ai/yam-teleop/current/.venv/bin/python /home/ai/yam-teleop/current/launch.py '
    return {
        'yam-teleop-agent.service': '[Unit]\nDescription=SZ_YAM02 standalone Teleop Agent\nPartOf=yam-teleop.target\n' + shared + 'ExecStart=' + prefix + 'agent\n\n[Install]\nWantedBy=yam-teleop.target\n',
        'yam-teleop-video@.service': '[Unit]\nDescription=SZ_YAM02 Teleop video %i\nPartOf=yam-teleop.target\n' + shared + 'ExecStart=' + prefix + 'video %i\n\n[Install]\nWantedBy=yam-teleop.target\n',
        'yam-teleop.target': '[Unit]\nDescription=SZ_YAM02 Teleop (explicit controller only)\n\n[Install]\nWantedBy=default.target\n',
    }


def install(deps, resume=False):
    if os.uname().nodename != 'frodobot' or Path.home() != Path('/home/ai'):
        raise RuntimeError('Wrong target host or user')
    baseline = json.loads((SOURCE / 'baseline.json').read_text())
    manifest = json.loads((SOURCE / 'manifest.json').read_text())
    verify_files(SOURCE, manifest['files'])
    protected_check(baseline)
    dep_manifest = json.loads((deps / 'manifest.json').read_text())
    verify_files(deps, dep_manifest)
    if (ROOT.exists() and not resume) or any((UNIT_DIR / u).exists() for u in units()):
        raise RuntimeError('Install destination/unit already exists; inspect before any replacement')
    if resume and (ROOT/'current').exists():
        raise RuntimeError('Resume is restricted to a failed, unpromoted installation')
    before = {u: subprocess.check_output(['systemctl','--user','show',u,
                 '--property=MainPID,ActiveState,SubState,UnitFileState'], text=True) for u in baseline['services']}
    os.nice(15)
    run('ionice', '-c', '3', '-p', str(os.getpid()))
    release = ROOT / 'releases/yam196-local-r1'
    release.parent.mkdir(parents=True, exist_ok=resume)
    if resume:
        old=json.loads((release/'manifest.json').read_text())
        verify_files(release,old['files'])
    shutil.copytree(SOURCE, release, dirs_exist_ok=resume)
    for name in ('config','runtime','logs','backups','bin'):
        (ROOT / name).mkdir(exist_ok=True)
    before_file=ROOT / 'backups/pre-install-services.json'
    if resume:
        before=json.loads(before_file.read_text())
        if sha(ROOT/'config/station.json') != sha(release/'defaults/station.json'):
            raise RuntimeError('Site config changed since failed installation')
    else:
        before_file.write_text(json.dumps(before, indent=2))
        shutil.copy2(release / 'defaults/station.json', ROOT / 'config/station.json')
    driver = release / 'vendor/i2rt'
    for relative in baseline['i2rt_files']:
        p = driver / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path('/home/ai/i2rt') / relative, p)
    verify_files(driver, baseline['i2rt_files'])
    constraints = release / 'constraints.txt'
    constraints.write_text('\n'.join(f'{n}=={v}' for n,v in baseline['packages'].items()
                                   if n.lower() not in ('i2rt','colosseum-client')) + '\n')
    uv = Path('/home/ai/.local/bin/uv')
    py = release / '.venv/bin/python'
    if not py.exists():
        run(uv, 'venv', '--python', '/usr/bin/python3.10', release / '.venv')
    offline = [uv, 'pip', 'install', '--offline', '--no-index', '--python', py, '--find-links', deps]
    run(*offline, 'flit_core==3.12.0', 'setuptools==84.0.0')
    run(*offline, '--no-build-isolation', '-c', constraints, driver,
        'omegaconf==2.3.1', 'websocket-client==1.9.0', 'pytest==9.1.1')
    run(uv, 'pip', 'check', '--python', py)

    private = release / 'deps'
    sysroot = private / 'ffmpeg'
    sysroot.mkdir(parents=True)
    for deb in deps.glob('*.deb'):
        run('dpkg-deb', '-x', deb, sysroot)
    safe_extract(deps / 'librealsense-2.58.4.tar.gz', private)
    rs_source = private / 'librealsense-2.58.4'
    api = (rs_source / 'include/librealsense2/rs.h').read_text()
    for key, value in [('RS2_API_MAJOR_VERSION',2),('RS2_API_MINOR_VERSION',58),('RS2_API_PATCH_VERSION',4)]:
        import re
        if not re.search(r'#define\s+'+key+r'\s+'+str(value)+r'\b', api):
            raise RuntimeError(f'RealSense SDK version mismatch: {key}')
    build = private / 'rs-build'
    sdk = private / 'realsense'
    # No dependency fetch permitted at build time. Dependencies must be in the bundle.
    env = dict(os.environ, http_proxy='http://127.0.0.1:9', https_proxy='http://127.0.0.1:9',
               HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9')
    safe_extract(deps / 'json-3.12.0.tar.gz', private)
    json_build = private / 'json-build'
    json_prefix = private / 'json'
    run('cmake', '-S', private / 'json-3.12.0', '-B', json_build,
        '-DJSON_BuildTests=OFF', f'-DCMAKE_INSTALL_PREFIX={json_prefix}', env=env)
    run('cmake', '--install', json_build, env=env)
    # Upstream performs an unconditional connectivity download even for offline builds.
    (rs_source / 'CMake/connectivity_check.cmake').write_text('set(INTERNET_CONNECTION OFF)\n')
    run('cmake', '-S', rs_source, '-B', build, '-DCMAKE_BUILD_TYPE=Release',
        f'-DCMAKE_INSTALL_PREFIX={sdk}', '-DCMAKE_INSTALL_LIBDIR=lib',
        '-DBUILD_EXAMPLES=OFF', '-DBUILD_GRAPHICAL_EXAMPLES=OFF', '-DBUILD_UNIT_TESTS=OFF',
        '-DBUILD_PYTHON_BINDINGS=OFF', '-DBUILD_WITH_DDS=OFF', '-DFORCE_RSUSB_BACKEND=OFF',
        '-DBUILD_ROSBAG2=OFF', '-DBUILD_TOOLS=OFF', '-DBUILD_GLSL_EXTENSIONS=OFF',
        '-DUSE_EXTERNAL_NLOHMANN_JSON=ON', f'-DCMAKE_PREFIX_PATH={json_prefix}',
        '-DLIBUSB_LIB=/usr/lib/x86_64-linux-gnu/libusb-1.0.so', '-DLIBUSB_INC=/usr/include/libusb-1.0',
        '-DCHECK_FOR_UPDATES=OFF', '-DFETCHCONTENT_FULLY_DISCONNECTED=ON', env=env)
    run('cmake', '--build', build, '--parallel', '1', env=env)
    run('cmake', '--install', build, env=env)
    ff_libs = ' '.join('/usr/lib/x86_64-linux-gnu/' + n for n in
                       ('libavcodec.so.58','libavutil.so.56','libswscale.so.5')) + ' -lm'
    run('make', '-j1', 'sender', 'fec_selftest', 'encode_cadence_selftest',
        f'FF_CFLAGS=-I{sysroot}/usr/include/x86_64-linux-gnu', f'FF_LIBS={ff_libs}',
        f'REALSENSE_CFLAGS=-I{sdk}/include',
        f'REALSENSE_LIBS=-L{sdk}/lib -lrealsense2 -Wl,-rpath,{sdk}/lib',
        cwd=release / 'low-latency-teleop', env=env)
    for name in ('fec_selftest','encode_cadence_selftest'):
        run(release / 'low-latency-teleop' / name)
    sys.path.insert(0, str(release / 'python'))
    # Configure uses PyYAML in the NEW venv, never the evaluation environment.
    run(py, '-c', f"import sys;sys.path.insert(0,{str(release / 'python')!r});from pathlib import Path;from yam_deploy.configure import render;render(Path({str(ROOT)!r}))")
    run(py, '-m', 'pytest', '-q', release / 'tests', release / 'piper-mate/Python_SDK/test_yam_remote_dual_server.py')
    run(py, release / 'verify_install.py', '--release', release, '--before-units')
    protected_check(baseline)
    ROOT.joinpath('current').symlink_to(release, target_is_directory=True)
    ctl = ROOT / 'bin/teleopctl'
    ctl.write_text('#!/bin/sh\nexec /home/ai/yam-teleop/current/.venv/bin/python /home/ai/yam-teleop/current/launch.py "$@"\n')
    ctl.chmod(0o755)
    for name, contents in units().items():
        with (UNIT_DIR / name).open('x') as f:
            f.write(contents)
    run('systemctl', '--user', 'daemon-reload')
    run(py, release / 'verify_install.py', '--release', release)
    after = {u: subprocess.check_output(['systemctl','--user','show',u,
                '--property=MainPID,ActiveState,SubState,UnitFileState'], text=True) for u in before}
    protected_check(baseline)
    report = {'installed': True, 'enabled': False, 'hardware_commissioned': False,
              'public_network_verified': False, 'evaluation_services_unchanged': before == after,
              'before': before, 'after': after}
    (ROOT / 'logs/install-result.json').write_text(json.dumps(report, indent=2) + '\n')
    if before != after:
        raise RuntimeError('Evaluation service state changed during install; inspect report (no automatic recovery)')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--deps', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    install(args.deps.resolve(), args.resume)
