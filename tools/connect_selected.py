"""Connect only the client explicitly chosen by this machine's owner."""
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'connect-agent/scripts'))
sys.path.insert(0, str(ROOT))


def existing_binding(data, base, host):
    """Refuse when this client's automation config belongs to another installation."""
    record_path = Path.home() / '.personal-memory' / 'automation' / host / 'config.json'
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text('utf-8-sig'))
    except (OSError, ValueError):
        return 'its existing automation config cannot be read'
    marker = record.get('data_marker')
    if marker:
        owned = Path(str(marker)).parent.resolve()
        if owned != data.resolve():
            return 'it is already connected to another Personal Memory installation'
    elif record.get('base_url') and record.get('base_url') != base:
        return 'it is already connected to another Personal Memory installation'
    return None


def main():
    print('Optional connections modify selected client settings and save per-client tokens locally.')
    print('1: Codex (read-only MCP)\n2: WorkBuddy (read-only MCP)\n3: OpenCode (read-only MCP)')
    print('4: Claude Code (MCP plus AUTOMATIC CAPTURE and task-enforcement hooks)\n0: Cancel')
    selected = input('Select one: ').strip()
    if selected == '0':
        return 0
    if selected not in {'1', '2', '3', '4'}:
        print('No client was selected.')
        return 1
    data = Path(os.environ['PERSONAL_MEMORY_DATA'])
    base = os.environ.get('PERSONAL_MEMORY_BASE', 'http://127.0.0.1:8765')
    host = 'claude' if selected == '4' else {'1': 'codex', '2': 'workbuddy', '3': 'opencode'}[selected]
    reason = existing_binding(data, base, host)
    if reason:
        print(host + ' was not changed: ' + reason + '.')
        print('Uninstall that installation first, or connect this installation with install.ps1 -Connect.')
        print('No client setting, token or hook was modified.')
        return 2
    if selected == '4':
        print('Claude capture stores conversation parts locally; configured extraction can send text to DeepSeek.')
        print('Task-enforcement hooks can deny actions until project grants and stage contracts are configured.')
        print('This is NOT an administrator write grant. Existing unrelated settings are preserved.')
        if input('Enable capture AND enforcement for Claude? Type YES: ') != 'YES':
            return 0
        import connect_claude
        result = connect_claude.install()
    else:
        print('Read-only MCP searches all authorized non-excluded active projects; it does not enable capture or direct Active writes.')
        if input('Change settings for ' + host + '? Type YES: ') != 'YES':
            return 0
        import automatic_install
        result = automatic_install.install_library_mcp(host, base, data)
    print('Selected connection configured. Restart/reload that client and confirm its own MCP trust prompt.')
    print('Configuration success is not proof of a native end-to-end interaction.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print('Connection not completed. Existing client/config conflicts must be reviewed locally; no token is printed.', file=sys.stderr)
        raise SystemExit(1)
