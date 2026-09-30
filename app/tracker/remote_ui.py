"""Open the home-server tracker through its dedicated local SSH tunnel."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import webbrowser

URL = 'http://127.0.0.1:8792'


def ssh_command(executable: str, key: Path) -> list[str]:
    return [executable, '-N', '-i', str(key), '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'KexAlgorithms=curve25519-sha256',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3', '-L', '127.0.0.1:8792:127.0.0.1:8791',
            'monolithic@192.168.1.50']


def healthy() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + '/healthz', timeout=2) as response:
            data = json.load(response)
        return data.get('service') == 'ARGUS Tracker' and data.get('automated_tracker') is True
    except (OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check connectivity without opening a browser')
    args = parser.parse_args()
    if not healthy():
        executable = shutil.which('ssh')
        key = Path.home() / '.ssh/monolith_server_ed25519'
        if not executable or not key.is_file():
            raise RuntimeError('OpenSSH and the existing home-server key are required; no credentials are guessed')
        root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ARGUS-Tracker-V2'
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'remote-ui-tunnel.log').open('ab') as log:
            process = subprocess.Popen(ssh_command(executable, key), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 30
        while not healthy():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('Home-server tunnel failed; inspect remote-ui-tunnel.log. No other browser or process was changed.')
            time.sleep(0.25)
    print('ARGUS server tracker reachable: ' + URL)
    if not args.check:
        webbrowser.open(URL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
