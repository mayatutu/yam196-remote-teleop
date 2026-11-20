"""Offline normal dual-Mate portable build; no device or network operations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = Path('E:/working/YAM/YAM')
CLIENT = SOURCE / 'arena-client'
OLD = CLIENT / 'dist/YAM Operator-video-health'


def run(argv, *, cwd, env=None, timeout=60):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} failed ({result.returncode}): '
                           f'{result.stdout[-2000:]} {result.stderr[-2000:]}')
    return result.stdout.strip()


def smoke(folder, work):
    env = dict(os.environ)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH',
                'QT_QPA_PLATFORM_PLUGIN_PATH', 'VLC_PLUGIN_PATH'):
        env.pop(key, None)
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    py = folder / 'runtime/python/python.exe'
    imported = run([py, '-B', '-c',
                    'import sys,pathlib,serial,fashionstar_uart_sdk,'
                    'remote_dual_client,arm_relay_client_bridge; '
                    'assert all(pathlib.Path(p).resolve().is_relative_to('
                    'pathlib.Path(sys.executable).parent.parent) for p in sys.path); '
                    'print("embedded Python and normal Mate imports OK")'],
                   cwd=work, env=env)
    for name in ('remote_dual_client.py', 'arm_relay_client_bridge.py'):
        run([py, '-B', folder / 'runtime/piper_sdk' / name, '--help'],
            cwd=work, env=env)
    vlc = folder / 'runtime/vlc'
    run([py, '-B', '-c',
         'import ctypes,os,sys; d=sys.argv[1]; '
         'handle=os.add_dll_directory(d); '
         'lib=ctypes.CDLL(os.path.join(d,"libvlc.dll")); '
         'lib.libvlc_get_version.restype=ctypes.c_char_p; '
         'print(lib.libvlc_get_version().decode())', vlc], cwd=work, env=env)
    receiver = folder / 'runtime/receiver/receiver.exe'
    out = work / 'receiver-smoke.log'
    with out.open('wb') as log:
        child = subprocess.Popen([str(receiver), '--mode', 'relay', '--relay-ip',
                                  '127.0.0.1', '--relay-port', '59994', '--session',
                                  'offline_packaging_check', '--name', 'head', '--headless'],
                                 cwd=work, env=env, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            time.sleep(2)
            if child.poll() is not None:
                raise RuntimeError(f'Receiver exited: {child.returncode}; {out.read_text(errors="replace")}')
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
    cfg = yaml.safe_load((folder / 'config/profiles.yaml').read_text(encoding='utf-8-sig'))
    cfg['state']['enabled'] = False
    cfg['state']['token'] = ''
    offline = work / 'offline-smoke.yaml'
    offline.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding='utf-8')
    env['QT_QPA_PLATFORM'] = 'offscreen'
    out = work / 'gui-smoke.log'
    with out.open('wb') as log:
        child = subprocess.Popen([str(folder / 'YAM Operator.exe'), '--config',
                                  str(offline), '--no-relay'], cwd=work, env=env,
                                 stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            time.sleep(5)
            if child.poll() is not None:
                raise RuntimeError(f'GUI exited: {child.returncode}; {out.read_text(errors="replace")}')
            text = out.read_text(errors='replace')
            if 'Traceback' in text or 'Failed to execute' in text:
                raise RuntimeError(text)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
    print(imported, flush=True)
    # Smoke generated local identity/logs are never delivered.
    (folder / 'runtime/client_id.txt').unlink(missing_ok=True)
    shutil.rmtree(folder / 'logs', ignore_errors=True)
    (folder / 'logs').mkdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--built-dir', type=Path,
                        help='Reuse this task\'s completed PyInstaller dist/YAM Operator folder')
    parser.add_argument('--resume-folder', type=Path, help='Resume verification of this task output')
    args = parser.parse_args()
    stamp = time.strftime('%Y%m%d-%H%M%S')
    work = HERE / '.build' / f'normal-portable-{stamp}'
    work.mkdir(parents=True, exist_ok=False)
    if args.resume_folder:
        built = args.resume_folder.resolve()
        if built.parent != (HERE.parent.parent / 'dist').resolve():
            raise RuntimeError('Resume must refer to this project dist directory')
    elif args.built_dir:
        built = args.built_dir.resolve()
        if not built.is_relative_to((HERE / '.build').resolve()):
            raise RuntimeError('Reuse is restricted to this task build directory')
    else:
        print('1/5 Building normal EXE offline...', flush=True)
        with (work / 'pyinstaller.log').open('wb') as log:
            subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                            '--distpath', str(work / 'dist'), '--workpath', str(work / 'work'),
                            str(CLIENT / 'yam_client/yam_operator/packaging/yam_operator.spec')],
                           cwd=CLIENT, stdout=log, stderr=log, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        built = work / 'dist/YAM Operator'
    if not (built / 'YAM Operator.exe').is_file():
        raise RuntimeError('PyInstaller build is not complete')
    release_root = HERE.parent.parent / 'dist'
    release_root.mkdir(exist_ok=True)
    folder = built if args.resume_folder else release_root / f'YAM Operator-normal-full-{stamp}'
    print('2/5 Adding embedded Python, normal SDK, receiver DLLs and VLC...', flush=True)
    if not args.resume_folder:
        shutil.copytree(built, folder)
    # Qt 6.11 uses Windows' unversioned ICU API. PyInstaller can collect an
    # unrelated MSYS2 ICU from PATH, whose exports have version suffixes.
    # Drop that collected collision so Windows loads its own system ICU.
    import pefile
    qt_core = pefile.PE(str(folder / '_internal/PySide6/Qt6Core.dll'), fast_load=True)
    qt_core.parse_data_directories(directories=[1])
    icu_names = {i.name for d in qt_core.DIRECTORY_ENTRY_IMPORT
                 if d.dll.lower() == b'icuuc.dll' for i in d.imports if i.name}
    candidate = folder / '_internal/icuuc.dll'
    if candidate.exists() and icu_names:
        icu = pefile.PE(str(candidate), fast_load=True)
        icu.parse_data_directories(directories=[0])
        exports = {s.name for s in icu.DIRECTORY_ENTRY_EXPORT.symbols}
        icu.close()
        if not icu_names.issubset(exports):
            system_icu = Path(os.environ['SystemRoot']) / 'System32/icuuc.dll'
            system_pe = pefile.PE(str(system_icu), fast_load=True)
            system_pe.parse_data_directories(directories=[0])
            assert icu_names.issubset({s.name for s in system_pe.DIRECTORY_ENTRY_EXPORT.symbols})
            system_pe.close()
            candidate.unlink()
            print('Removed incompatible collected ICU; using Windows system ICU.', flush=True)
    qt_core.close()
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '*.log', '*.pem', '*.key')
    for name in ('python', 'receiver', 'vlc'):
        if not args.resume_folder:
            shutil.copytree(OLD / 'runtime' / name, folder / 'runtime' / name, ignore=ignore)
    sdk = folder / 'runtime/piper_sdk'
    if not args.resume_folder:
        shutil.copytree(SOURCE / 'piper-mate/Python_SDK', sdk, ignore=ignore)
    (folder / 'config').mkdir(exist_ok=True)
    profile = CLIENT / 'yam_client/yam_operator/config/profiles.packaged.yaml'
    shutil.copy2(profile, folder / 'config/profiles.yaml')
    cfg = yaml.safe_load(profile.read_text(encoding='utf-8-sig'))
    assert cfg['mate']['left_com'] != cfg['mate']['right_com']
    for value in cfg['paths'].values():
        assert not Path(value).is_absolute() and (folder / value).exists(), value
    (folder / 'logs').mkdir(exist_ok=True)
    (folder / '使用说明.txt').write_text(
        '正常双 Mate 完整便携版（Windows 64位）\n\n'
        '1. 完整解压整个文件夹，不要单独移动EXE。\n'
        '2. 双击 YAM Operator.exe，无需安装Python或VLC。\n'
        '3. 接上两只Mate，扫描并选择左右COM，应用并重连。\n'
        '4. 配置位于config/profiles.yaml，沿用原版State/Relay地址和认证方式。\n'
        '   若目标电脑未配置站点认证，需使用现有OPERATOR_STATE_TOKEN。\n'
        '5. runtime与_internal是程序运行文件，务必完整保留。\n\n'
        '本包为正常双Mate版本，无单Mate镜像测试入口；未改变原控制速度。\n',
        encoding='utf-8-sig')
    print('3/5 Checking independent offline startup (no devices)...', flush=True)
    smoke(folder, work)
    files = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(folder.rglob('*')) if p.is_file()}
    assert not any('mirror_mate_test' in n or 'open_operator_mirror_test' in n
                   or n.endswith(('.key', 'id_rsa.pem', 'robotic-arm.pem')) for n in files)
    for name in files:
        if name.endswith('.pem'):
            assert 'PRIVATE KEY' not in (folder / name).read_text(errors='replace'), name
    manifest = {'version': 'normal-full', 'normal_dual_mate': True,
                'offline_runtime_checks': 'passed', 'files': files}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = folder.with_suffix('.zip')
    print('4/5 Creating full portable ZIP...', flush=True)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=4) as z:
        for p in sorted(folder.rglob('*')):
            z.write(p, p.relative_to(folder.parent))
    print('5/5 Extracting and checking relocated package...', flush=True)
    extracted = work / '解压验证'
    with zipfile.ZipFile(archive) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f'ZIP CRC mismatch: {bad}')
        z.extractall(extracted)
    relocated = extracted / folder.name
    for name, digest in files.items():
        assert hashlib.sha256((relocated / name).read_bytes()).hexdigest() == digest, name
    smoke(relocated, work)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    result = {'zip': str(archive), 'folder': str(folder), 'bytes': archive.stat().st_size,
              'sha256': digest, 'files': len(files), 'relocated_smoke': 'passed',
              'hardware_exercised': False}
    (work / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
