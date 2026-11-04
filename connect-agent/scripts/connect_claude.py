"""Connect Claude Code to the local Personal Memory service without printing credentials.

The adapter uses Claude Code's user-level UserPromptSubmit/Stop hooks for automatic
project-scoped reads and turn capture, plus a separate read-only MCP credential.
It never copies another host's credential and never prints a token.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import winreg
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")))
BASE = os.environ.get("PERSONAL_MEMORY_BASE", "http://127.0.0.1:8765")
HOME = Path.home()
FOLDER = HOME / ".personal-memory/automation/claude"
CONFIG = FOLDER / "config.json"
MANIFEST = FOLDER / "install.json"
SETTINGS = HOME / ".claude/settings.json"
CLAUDE_JSON = HOME / ".claude.json"
PYTHON = ROOT / ".venv/Scripts/python.exe"
HOOK = ROOT / "connect-agent/runtime/hook.py"
BRIDGE = ROOT / "workbuddy_readonly_bridge.py"
AUTO_ENV = "PERSONAL_MEMORY_AUTO_CLAUDE_TOKEN"
MCP_ENV = "PERSONAL_MEMORY_MCP_CLAUDE_TOKEN"
HOST = "claude"
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse")


def read(path: Path):
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else {}


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf8")
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def env_get(name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def env_set(name: str, value) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def claude_exe():
    found = shutil.which("claude")
    if found:
        return Path(found)
    base = HOME / "AppData/Local/Claude-3p/claude-code"
    candidates = sorted(base.rglob("claude.exe"), key=lambda path: path.stat().st_mtime) if base.is_dir() else []
    return candidates[-1] if candidates else None


def call(opener, path: str, payload=None, csrf=None, method=None, headers=None):
    request_headers = {"Content-Type": "application/json", "Origin": BASE, **(headers or {})}
    if csrf:
        request_headers["x-csrf-token"] = csrf
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf8") if payload is not None else None,
        headers=request_headers,
        method=method,
    )
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def session():
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    csrf = call(opener, "/api/session", {"key": (DATA / "admin-key.txt").read_text("ascii").strip()})["csrf_token"]
    return opener, csrf


def hook_handler():
    return {
        "type": "command",
        "command": str(PYTHON),
        "args": ["-X", "utf8", str(HOOK), "--config", str(CONFIG)],
        "timeout": 10,
    }


def hook_group():
    return {"hooks": [hook_handler()]}


def _same(left, right) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)


def merge_hooks(settings, group):
    result = json.loads(json.dumps(settings))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude settings hooks is not an object")
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("Claude settings hook event is not a list")
        if not any(_same(entry, group) for entry in entries):
            entries.append(group)
    return result


def _owned_group(group) -> bool:
    if not isinstance(group, dict):
        return False
    haystack = json.dumps(group, ensure_ascii=False).casefold()
    return str(HOOK).casefold() in haystack and str(CONFIG).casefold() in haystack


def remove_hooks(settings, group):
    result = json.loads(json.dumps(settings))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    removed = 0
    for event in HOOK_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not (_same(entry, group) or _owned_group(entry))]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return result, removed


def mcp_entry():
    return {
        "type": "stdio",
        "command": str(PYTHON),
        "args": [str(BRIDGE), "--token-env", MCP_ENV, "--url", BASE + "/mcp/"],
    }


def _run_claude(args):
    executable = claude_exe()
    if not executable:
        raise RuntimeError("Claude Code CLI was not found")
    return subprocess.run(
        [str(executable), *args],
        capture_output=True,
        text=True,
        encoding="utf8",
        errors="replace",
        timeout=60,
    )


def mcp_configured():
    executable = claude_exe()
    if not executable:
        return False
    result = _run_claude(["mcp", "get", "personal_memory"])
    return result.returncode == 0


def install_mcp():
    if mcp_configured():
        current = _run_claude(["mcp", "get", "personal_memory"]).stdout
        if str(BRIDGE).casefold() in current.casefold() and MCP_ENV in current:
            return {"changed": False, "target": "user"}
        raise ValueError("An existing personal_memory MCP entry differs; inspect before replacing")
    entry = json.dumps(mcp_entry(), ensure_ascii=False)
    result = _run_claude(["mcp", "add-json", "--scope", "user", "personal_memory", entry])
    if result.returncode:
        raise RuntimeError("Claude MCP registration failed: " + (result.stderr or result.stdout).strip()[:300])
    return {"changed": True, "target": "user"}


def remove_mcp():
    if not mcp_configured():
        return {"removed": False, "reason": "absent"}
    result = _run_claude(["mcp", "remove", "--scope", "user", "personal_memory"])
    if result.returncode:
        raise RuntimeError("Claude MCP removal failed: " + (result.stderr or result.stdout).strip()[:300])
    return {"removed": True}


def _sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_settings():
    FOLDER.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = FOLDER / f"settings-{stamp}.json"
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
    return backup


def _write_settings(settings):
    atomic_json(SETTINGS, settings)


def _restore_settings(backup, existed):
    if backup is not None and backup.exists():
        shutil.copy2(backup, SETTINGS)
    elif backup is not None and not existed:
        SETTINGS.unlink(missing_ok=True)


def _restrict_outbox(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, encoding="utf8", errors="replace"
    )
    user = result.stdout.strip().split(",")[-1].strip('"')
    check = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
        capture_output=True,
        text=True,
        encoding="utf8",
        errors="replace",
    )
    if check.returncode:
        raise RuntimeError("Could not restrict automatic outbox permissions")


def _diagnostic(token, client_id):
    request = urllib.request.Request(
        BASE + "/automation/v2/context",
        data=json.dumps({
            "session_id": "installation-claude-" + uuid.uuid4().hex,
            "request_id": uuid.uuid4().hex,
            "cwd": str(ROOT),
            "prompt": "installation diagnostic",
        }).encode("utf8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        result = json.load(response)
    return {
        "client_id": client_id,
        "mode": result.get("mode"),
        "items": len(result.get("items", [])),
        "scope_project_id": (result.get("scope") or {}).get("project_id"),
        "elapsed_ms": result.get("elapsed_ms"),
        "trace_id": result.get("trace_id"),
    }


def status():
    config = read(CONFIG)
    settings = read(SETTINGS)
    hooks = settings.get("hooks", {}) if isinstance(settings.get("hooks"), dict) else {}
    group = hook_group()
    present = {
        event: any(_same(entry, group) or _owned_group(entry) for entry in hooks.get(event, []))
        if isinstance(hooks.get(event), list) else False
        for event in HOOK_EVENTS
    }
    return {
        "host": HOST,
        "config": str(CONFIG),
        "config_present": CONFIG.exists(),
        "enabled": bool(config.get("enabled")),
        "client_id": config.get("client_id"),
        "auto_token_present": bool(env_get(AUTO_ENV)),
        "mcp_token_present": bool(env_get(MCP_ENV)),
        "hooks_present": present,
        "mcp_present": mcp_configured(),
        "manifest": str(MANIFEST) if MANIFEST.exists() else None,
    }


def install():
    if not PYTHON.is_file() or not HOOK.is_file() or not BRIDGE.is_file():
        raise ValueError("Claude adapter runtime is missing")
    if not (DATA / ".personal-memory-data").is_file():
        raise ValueError("Personal Memory data marker is missing")
    if CONFIG.exists():
        current = status()
        core_ready = (
            current["enabled"]
            and current["mcp_present"]
            and current["auto_token_present"]
            and current["mcp_token_present"]
        )
        if core_ready:
            settings = read(SETTINGS)
            merged = merge_hooks(settings, hook_group())
            changed = merged != settings
            backup = _backup_settings() if changed else None
            if changed:
                _write_settings(merged)
            installed = read(MANIFEST)
            if installed:
                installed.update({
                    "settings_backup": str(backup) if backup else installed.get("settings_backup"),
                    "settings_sha256": _sha256(SETTINGS),
                    "hook_group": hook_group(),
                })
                atomic_json(MANIFEST, installed)
                atomic_json(FOLDER / "installation.json", installed)
            return {"idempotent": not changed, "hooks_upgraded": changed, **status()}
        raise ValueError("Existing Claude configuration is incomplete; run status or uninstall first")
    if env_get(AUTO_ENV) or env_get(MCP_ENV):
        raise ValueError("Unowned Claude credentials exist; refusing replacement")

    opener, csrf = session()
    settings_existed = SETTINGS.exists()
    created, saved_env, written_settings, mcp_added = [], [], None, False
    try:
        backup = call(opener, "/api/backup", {}, csrf)
        auto = call(opener, "/api/automation/library-clients", {"app": HOST, "excluded_roots": [], "purpose": "automation"}, csrf)
        created.append(auto["client_id"])
        reader = call(
            opener,
            "/api/automation/library-clients",
            {"app": HOST, "excluded_roots": [], "purpose": "mcp"},
            csrf,
            headers={"x-personal-memory-installer": "local-installer"},
        )
        created.append(reader["client_id"])
        env_set(AUTO_ENV, auto.pop("token"))
        saved_env.append(AUTO_ENV)
        env_set(MCP_ENV, reader.pop("token"))
        saved_env.append(MCP_ENV)

        outbox = DATA / "automation-outbox" / auto["client_id"]
        _restrict_outbox(outbox)
        config = {
            "enabled": True,
            "app": HOST,
            "base_url": BASE,
            "client_id": auto["client_id"],
            "access_mode": "library",
            "binding_id": None,
            "token_env": AUTO_ENV,
            "roots": [],
            "excluded_roots": [],
            "state_dir": str(FOLDER / "state"),
            "outbox_dir": str(outbox),
            "data_marker": str(DATA / ".personal-memory-data"),
            "transcript_roots": [str(HOME / ".claude/projects")],
            "injection_audit_enabled": False,
        }
        atomic_json(CONFIG, config)

        written_settings = _backup_settings()
        settings = merge_hooks(read(SETTINGS), hook_group())
        _write_settings(settings)
        mcp_added = install_mcp()["changed"]

        diagnostic = _diagnostic(env_get(AUTO_ENV), auto["client_id"])
        installed = {
            "host": HOST,
            "installed_at": datetime.now().astimezone().isoformat(),
            "config": str(CONFIG),
            "client_id": auto["client_id"],
            "mcp_client_id": reader["client_id"],
            "outbox": str(outbox),
            "backup_id": backup["backup_id"],
            "settings_backup": str(written_settings),
            "settings_sha256": _sha256(SETTINGS),
            "hook_group": hook_group(),
            "mcp_entry": mcp_entry(),
            "mcp_added": mcp_added,
            "diagnostic": diagnostic,
            "real_claude_chat": "NOT_RUN",
        }
        atomic_json(MANIFEST, installed)
        (FOLDER / "installation.json").write_text(
            json.dumps(installed, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        return {"configuration_saved": True, **{k: v for k, v in installed.items() if k not in {"hook_group", "mcp_entry"}}, "status": status()}
    except Exception:
        try:
            if mcp_added:
                remove_mcp()
        except Exception:
            pass
        _restore_settings(written_settings, settings_existed)
        CONFIG.unlink(missing_ok=True)
        MANIFEST.unlink(missing_ok=True)
        for name in saved_env:
            env_set(name, None)
        for client_id in reversed(created):
            try:
                call(opener, "/api/automation/clients/" + client_id, {"enabled": False, "revoke": True}, csrf, "PATCH")
            except Exception:
                pass
        raise
    finally:
        call(opener, "/api/session", None, csrf, "DELETE")


def uninstall():
    config = read(CONFIG)
    installed = read(MANIFEST)
    settings = read(SETTINGS)
    restored, removed_hooks = remove_hooks(settings, hook_group())
    if removed_hooks:
        backup = _backup_settings()
        _write_settings(restored)
    else:
        backup = None
    mcp = remove_mcp()
    opener, csrf = session()
    revoked = []
    try:
        for client_id in (config.get("client_id"), installed.get("mcp_client_id")):
            if client_id:
                try:
                    call(opener, "/api/automation/clients/" + client_id, {"enabled": False, "revoke": True}, csrf, "PATCH")
                    revoked.append(client_id)
                except Exception:
                    pass
    finally:
        call(opener, "/api/session", None, csrf, "DELETE")
    for name in (AUTO_ENV, MCP_ENV):
        env_set(name, None)
    if CONFIG.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        CONFIG.rename(CONFIG.with_name(f"config.uninstalled-{stamp}.json"))
    return {
        "host": HOST,
        "hooks_removed": removed_hooks,
        "settings_backup": str(backup) if backup else None,
        "mcp": mcp,
        "revoked_clients": revoked,
        "config_archived": not CONFIG.exists(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "status", "uninstall"))
    args = parser.parse_args()
    result = {"install": install, "status": status, "uninstall": uninstall}[args.command]()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
