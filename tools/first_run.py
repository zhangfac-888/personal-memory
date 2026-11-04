"""Local first-run setup. No network calls, no secrets in output or arguments."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def initialize(data, port):
    from backend.installation import register_installation
    data = data.resolve()
    identity = data / 'installation.json'
    if identity.exists():
        previous = json.loads(identity.read_text('utf8'))
        if (Path(previous['source_root']).resolve() != ROOT or Path(previous['data_root']).resolve() != data
                or previous['port'] != port):
            raise ValueError('Different existing instance; migration is not automatic')
    elif (data / 'memory.db').exists():
        raise ValueError('Existing library has no installation identity; refusing to adopt it')
    value = register_installation(data, port, ROOT)
    value['discovery_enabled'] = False
    identity.write_text(json.dumps(value, indent=2), 'utf8')
    print('Instance initialized. Automatic host discovery remains disabled.')


def configure_key(data):
    from backend.credentials import transform
    if os.name != 'nt':
        raise ValueError('Credential storage requires Windows DPAPI')
    data = data.resolve()
    if not (data / 'installation.json').is_file():
        raise ValueError('Initialize this instance first')
    folder = data / 'secrets'
    folder.mkdir(exist_ok=True)
    path = folder / 'deepseek-key.dpapi'
    if path.exists() and input('Replace the existing encrypted key? Type YES: ') != 'YES':
        print('Existing key preserved.')
        return
    key = getpass.getpass('Your DeepSeek API key (hidden; Enter to cancel): ').strip()
    if not key:
        print('Skipped. AI extraction is not configured by this step.')
        return
    if len(key) > 512 or any(ch.isspace() for ch in key):
        raise ValueError('Invalid credential shape')
    principal = os.environ.get('USERDOMAIN', '') + '\\' + getpass.getuser()
    subprocess.run(['icacls.exe', str(folder), '/inheritance:r', '/grant:r', principal + ':(OI)(CI)F'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    encrypted = transform(key.encode('utf8'))
    key = None
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
            output.write(encrypted)
            temp = Path(output.name)
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    print('Key saved with Windows machine-scoped DPAPI and restricted folder ACLs. No API request was sent.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('init', 'configure-key'))
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not args.data_dir.is_absolute():
        parser.error('Choose an absolute dedicated data directory')
    try:
        if args.command == 'init':
            initialize(args.data_dir, args.port)
        else:
            configure_key(args.data_dir)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('Setup failed. Check the dedicated data directory, existing identity and Windows permissions; no credential is printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
