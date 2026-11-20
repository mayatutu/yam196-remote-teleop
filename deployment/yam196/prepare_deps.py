"""Explicitly approved dependency download; only writes .build/deps locally."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
DEPS = HERE / '.build/deps'
DEBS = {
 'libavcodec-dev': 'ab8cb22198f8169f4c9d394207d8898eb61d0a3f83cdc54121a3b996f72dea448ebf6250820c01e2589ab8d8fc7d9cbdecbefa5315139ae90e01bb2d187ed7bb',
 'libavutil-dev': '7d5290a8b9421626d8994780d9495a6fa66c275313330c309b279a89425128ba4e4e61959fdce6056664a4a137e5cb0c0b705f6e518749248595559a8fecc40f',
 'libswscale-dev': '13a460d8f13ed7894ebd8721221bf93b69d7528ffa6fefd086833e2a4efb7fe4256a6a9615cdfcf0a19f9d30d1c78769e0fbb1d3c2555c64ba5798fdcaad9304',
 'libswresample-dev': '6efa00974242e7975690a997965dca5bb425c8f09354444b4f4134fc5f767c66693cca6972fc382c702126bf5e2a903b6011f94aad13d9344a1e898ebbf28a00',
}


def download(url, path):
    if path.exists():
        return
    request = urllib.request.Request(url, headers={'User-Agent': 'YAM-deployment/1'})
    with urllib.request.urlopen(request, timeout=45) as response, path.with_suffix('.partial').open('wb') as f:
        while block := response.read(1024 * 1024):
            f.write(block)
    path.with_suffix('.partial').replace(path)


def main():
    DEPS.mkdir(parents=True, exist_ok=True)
    if '--json-only' in sys.argv:
        download('https://codeload.github.com/nlohmann/json/tar.gz/refs/tags/v3.12.0', DEPS / 'json-3.12.0.tar.gz')
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEPS.iterdir()
                 if p.is_file() and p.name != 'manifest.json' and p.suffix != '.partial'}
        (DEPS / 'manifest.json').write_text(json.dumps(files, indent=2) + '\n')
        print('Downloaded fixed RealSense transitive dependency: nlohmann/json 3.12.0')
        return
    for name, expected in DEBS.items():
        filename = f'{name}_4.4.2-0ubuntu0.22.04.1_amd64.deb'
        path = DEPS / filename
        download('https://cn.archive.ubuntu.com/ubuntu/pool/universe/f/ffmpeg/' + filename, path)
        if hashlib.sha512(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'APT metadata checksum mismatch: {name}')
    download('https://codeload.github.com/realsenseai/librealsense/tar.gz/refs/tags/v2.58.4',
             DEPS / 'librealsense-2.58.4.tar.gz')
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-deps', '--dest', str(DEPS),
                    'omegaconf==2.3.1', 'websocket-client==1.9.0',
                    'antlr4-python3-runtime==4.9.3', 'flit_core==3.12.0'], check=True)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEPS.iterdir()
             if p.is_file() and p.name != 'manifest.json' and p.suffix != '.partial'}
    (DEPS / 'manifest.json').write_text(json.dumps(files, indent=2) + '\n')
    print(json.dumps({'downloaded_files': len(files), 'output': str(DEPS)}))


if __name__ == '__main__':
    main()
