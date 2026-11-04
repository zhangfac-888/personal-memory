"""Install only this component's hooks, preserving all unrelated client settings."""
from __future__ import annotations
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
import urllib.request
import winreg

ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_PYTHON = ROOT / ".venv/Scripts/python.exe"
PYTHON = _BUNDLED_PYTHON if _BUNDLED_PYTHON.is_file() else Path(sys.executable).resolve()
HOOK = ROOT / "connect-agent/runtime/hook.py"
PROFILE_ROOT = Path.home()
INSTALL_ROOT = PROFILE_ROOT / ".personal-memory/automation"
DATA = Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")))
BASE = os.environ.get("PERSONAL_MEMORY_BASE", "http://127.0.0.1:8765")
HOSTS = ("workbuddy", "codex", "opencode", "qoder")
BASE_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
CODEX_HOOK_EVENTS = ("UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse")
DISCOVERY_INTERVAL = 600
DISCOVERY_HISTORY_MAX_LINES = 500
DISCOVERY_HISTORY_MAX_BYTES = 1_000_000
ERROR_DETAIL_LIMIT = 600
ENV_STORE = None  # Tests may inject a dict; isolated profiles never use real HKCU.


def host_hook_events(host):
    return CODEX_HOOK_EVENTS if host == "codex" else BASE_HOOK_EVENTS

import importlib.util
_safe_spec = importlib.util.spec_from_file_location("personal_memory_safe_mcp", Path(__file__).with_name("safe_mcp_repair.py"))
MCP_SAFE = importlib.util.module_from_spec(_safe_spec)
_safe_spec.loader.exec_module(MCP_SAFE)


def read(path):
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data)
        temporary = Path(f.name)
    os.replace(temporary, path)


def safe_error_detail(error):
    """Return a bounded, token-free explanation for discovery diagnostics."""
    detail = " ".join(str(error).split())
    detail = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(r"(?i)((?:token|secret|password|authorization)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]", detail)
    return detail[:ERROR_DETAIL_LIMIT]


def error_fields(error, stage, target=None, manifest=None, hook_target=None, mcp_target=None):
    """Stable diagnostic fields; keep the original class name for compatibility."""
    return {"error": type(error).__name__, "error_type": type(error).__name__,
        "error_detail": safe_error_detail(error), "stage": stage,
        "target": str(target) if target is not None else None,
        "manifest": str(manifest) if manifest is not None else None,
        "hook_target": str(hook_target) if hook_target is not None else None,
        "mcp_target": str(mcp_target) if mcp_target is not None else None}


def append_discovery_history(path, record, max_lines=DISCOVERY_HISTORY_MAX_LINES,
        max_bytes=DISCOVERY_HISTORY_MAX_BYTES):
    """Append one bounded history line; history is evidence, not a second state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf8") as stream:
        stream.write(line)
    if path.stat().st_size <= max_bytes:
        return
    lines = path.read_text("utf8").splitlines()[-max_lines:]
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(("\n".join(lines) + ("\n" if lines else "")).encode("utf8"))
        temporary = Path(f.name)
    os.replace(temporary, path)


def config_target(host):
    if host == "workbuddy":
        return PROFILE_ROOT / ".workbuddy/settings.json"
    if host == "qoder":
        return PROFILE_ROOT / ".qoder-cn/settings.json"
    if host == "codex":
        return PROFILE_ROOT / ".codex/hooks.json"
    if host == "opencode":
        return PROFILE_ROOT / ".config/opencode/plugins/personal-memory-automatic.js"
    raise ValueError("Unsupported host")


def opencode_hook_python():
    """The stdlib-only hook can bypass Windows' extra venv launcher process."""
    try:
        result = subprocess.run([str(PYTHON), "-I", "-S", "-c", "import sys;print(sys._base_executable)"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        base = Path(result.stdout.strip())
        if result.returncode == 0 and base.is_absolute() and base.is_file():
            return base.resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return PYTHON


def hook_group(config_path):
    command = f'"{PYTHON}" -X utf8 "{HOOK}" --config "{config_path}"'
    handler={"type":"command","command":command,"timeout":2}
    if config_path.parent.name=="codex":
        handler["commandWindows"]="& "+command
    return {"hooks":[handler]}


def merge_hooks(existing, group, events=BASE_HOOK_EVENTS):
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks is not an object")
    for event in events:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("Existing hook event is not a list")
        if group not in entries:
            entries.append(group)
    return result


def call(opener, base, path, method="GET", payload=None, csrf=None, headers=None):
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if csrf:
        request_headers["x-csrf-token"] = csrf
    data = json.dumps(payload).encode("utf8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=request_headers, method=method)
    with opener.open(req, timeout=15) as response:
        return json.load(response)


def session(base, data):
    parsed=urllib.parse.urlparse(base)
    if parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("Installation must use a local loopback service")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = call(opener, base, "/api/session", "POST", {"key": (data / "admin-key.txt").read_text("ascii").strip()})["csrf_token"]
    return opener, csrf


def env_get(name):
    if ENV_STORE is not None:
        return ENV_STORE.get(name)
    if PROFILE_ROOT.resolve() != Path.home().resolve():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def env_set(name, value):
    if ENV_STORE is not None:
        ENV_STORE[name] = value
        return
    if PROFILE_ROOT.resolve() != Path.home().resolve():
        raise RuntimeError("Isolated profiles require an isolated credential store")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def env_delete(name):
    if ENV_STORE is not None:
        ENV_STORE.pop(name, None)
        return
    if PROFILE_ROOT.resolve() != Path.home().resolve():
        raise RuntimeError("Isolated profiles cannot modify real user credentials")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        try: winreg.DeleteValue(key, name)
        except FileNotFoundError: pass


def detect(host):
    target = config_target(host)
    config = INSTALL_ROOT / host / "config.json"
    status = INSTALL_ROOT / host / "state/status.json"
    mcp_manifest = INSTALL_ROOT / host / "mcp.json"
    try:
        current = read(config)
        return {"host": host, "inspection_state": "ok", "config_target_exists": target.exists(),
            "automatic_installed": config.exists(), "enabled": current.get("enabled", False),
            "access_mode": current.get("access_mode", "unknown"), "mcp_installed": mcp_manifest.exists(),
            "last_event": read(status),
            "real_desktop_chat": "NOT_RUN" if not status.exists() else "check_event_evidence"}
    except PermissionError as error:
        # An unreadable host is unknown, never proof of absence or safe to repair.
        return {"host": host, "inspection_state": "unreadable", "config_target_exists": None,
            "automatic_installed": None, "enabled": None, "access_mode": "unknown",
            "mcp_installed": None, "last_event": None, "real_desktop_chat": "NOT_RUN",
            "inspection_error": safe_error_detail(error)}


def known_host_installed(host):
    if config_target(host).exists():
        return True
    profiles={"codex":".codex","workbuddy":".workbuddy","opencode":".config/opencode","qoder":".qoder-cn"}
    known={
        "codex":[PROFILE_ROOT/"AppData/Local/Programs/Codex/Codex.exe"],
        "workbuddy":[PROFILE_ROOT/"AppData/Local/Programs/WorkBuddy/WorkBuddy.exe"],
        "opencode":[PROFILE_ROOT/"AppData/Local/Programs/OpenCode/OpenCode.exe"],
        "qoder":[PROFILE_ROOT/"AppData/Local/Programs/Qoder CN/Qoder CN.exe"],
    }
    return bool(shutil.which(host) or (PROFILE_ROOT/profiles[host]).is_dir() or any(p.is_file() for p in known[host]))


def guard_install_target(base, data):
    """Reject accidental test endpoints before touching a real user profile."""
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("Only a local loopback service is supported")
    if PROFILE_ROOT.resolve() != Path.home().resolve():
        # Isolated profiles are supplied by tests; no real registry writes allowed.
        return
    for host in HOSTS:
        existing = read(INSTALL_ROOT / host / "config.json")
        marker = existing.get("data_marker")
        if marker and (Path(marker).parent.resolve() != Path(data).resolve() or existing.get("base_url") != base):
            raise ValueError("Installation target differs from existing client; use a verified migration")


def discover_and_repair(base=None, data=None):
    base = base or BASE
    data = Path(data) if data else DATA
    guard_install_target(base, data)
    """Inspect known host locations, then safely reconcile owned entries."""
    results=[]
    for host in HOSTS:
        stage="detect"
        hook_target=config_target(host)
        mcp_target=_mcp_target(host)
        manifest=INSTALL_ROOT/host/"mcp.json"
        try:
            current=detect(host)
        except Exception as error:
            results.append({"host":host,"state":"repair_failed",
                **error_fields(error,stage,hook_target,manifest,hook_target,mcp_target)})
            continue
        if current.get("inspection_state") == "unreadable":
            results.append({"host":host,"state":"unreadable","stage":"detect",
                "target":str(hook_target),"manifest":str(manifest),
                "hook_target":str(hook_target),"mcp_target":str(mcp_target),
                "error_detail":current.get("inspection_error")})
            continue
        if current["automatic_installed"] and not current["enabled"]:
            results.append({"host":host,"state":"paused"})
            continue
        if not current["automatic_installed"] and not known_host_installed(host):
            results.append({"host":host,"state":"not_installed"})
            continue
        try:
            stage="install"
            installed=install(host,base,data)
            mcp_result = None
            if installed.get("configuration_saved"):
                stage="mcp"
                mcp_result = install_library_mcp(host,base,data)
            idempotent = bool(installed.get("idempotent")) and (mcp_result is None or bool(mcp_result.get("idempotent")))
            results.append({"host":host,"state":"configured" if idempotent else "repaired" if current["automatic_installed"] else "installed_after_discovery",
                "idempotent":idempotent,"mcp_changed":bool(mcp_result and mcp_result.get("changed"))})
        except (ValueError,RuntimeError) as error:
            error_target = mcp_target if stage=="mcp" else hook_target
            results.append({"host":host,"state":"configuration_conflict",
                **error_fields(error,stage,error_target,manifest,hook_target,mcp_target)})
        except Exception as error:
            error_target = mcp_target if stage=="mcp" else hook_target
            results.append({"host":host,"state":"repair_failed",
                **error_fields(error,stage,error_target,manifest,hook_target,mcp_target)})
    return results


def start_discovery_worker(base=None, data=None):
    base = base or BASE
    data = Path(data) if data else DATA
    """Low-frequency, bounded discovery in the existing service process."""
    # Keep the worker's evidence beside the database the server reports on,
    # including isolated tests. The old per-profile location was never read by
    # /api/status and made a healthy worker look absent.
    status_path = Path(data) / "discovery-status.json"
    history_path = Path(data) / "discovery-history.jsonl"

    def run():
        while True:
            checked_at = datetime.now().astimezone().isoformat()
            record = {"checked_at": checked_at, "hosts": [], "scope": "known_supported_config_paths_only"}
            try:
                record["hosts"] = discover_and_repair(base, data)
            except Exception as error:
                # Keep the worker failure visible instead of silently retrying.
                record["worker_error"] = error_fields(error, "discovery")
            try:
                write(status_path, record)
            except Exception:
                pass
            try:
                append_discovery_history(history_path, record)
            except Exception:
                pass
            time.sleep(DISCOVERY_INTERVAL)

    thread = threading.Thread(target=run, name="personal-memory-discovery", daemon=True)
    thread.start()
    return thread


def _toml_section_bounds(text, name):
    return MCP_SAFE.toml_bounds(text, name)


def _mcp_target(host):
    if host == "workbuddy":
        return PROFILE_ROOT / ".workbuddy/mcp.json"
    if host == "qoder":
        return PROFILE_ROOT / ".qoder-cn/mcp.json"
    if host == "codex":
        return PROFILE_ROOT / ".codex/config.toml"
    if host == "opencode":
        return PROFILE_ROOT / ".config/opencode/opencode.jsonc"
    raise ValueError("Unsupported MCP host")



def expected_mcp_entry(host, base, env_name):
    if host == "codex":
        return {"url":base+"/mcp/", "bearer_token_env_var":env_name}
    if host == "opencode":
        return {"type":"remote", "url":base+"/mcp/", "enabled":True, "oauth":False,
            "headers":{"Authorization":"Bearer {env:"+env_name+"}"}}
    return {"type":"stdio", "command":str(PYTHON),
        "args":[str(ROOT/"workbuddy_readonly_bridge.py"),"--token-env",env_name,"--url",base+"/mcp/"], "timeout":30000}


def mcp_actual(host, raw):
    if host == "codex":
        import tomllib
        current = tomllib.loads((raw or b"").decode("utf-8-sig"))
        entries = current.get("mcp_servers", {})
        if not isinstance(entries, dict):
            raise ValueError("MCP configuration section must be a table")
        return "personal_memory" in entries, entries.get("personal_memory")
    return MCP_SAFE.json_entry(raw, "mcpServers" if host in {"workbuddy","qoder"} else "mcp", host == "opencode")


def mcp_patch(host, raw, expected):
    if host == "codex":
        return MCP_SAFE.patch_toml(raw, expected)
    return MCP_SAFE.patch_json(raw, "mcpServers" if host in {"workbuddy","qoder"} else "mcp", expected, host == "opencode")


def validate_mcp_manifest(host, manifest, target, base, data, env_name):
    if (manifest.get("host") != host or manifest.get("env_name") != env_name
            or manifest.get("target") != str(target) or manifest.get("mode") != "library-read-only"
            or not manifest.get("client_id")):
        raise ValueError("Installed MCP identity or target changed; refusing credential redirection")
    for key, value in {"base_url":base, "data_marker":str(Path(data)/".personal-memory-data"), "purpose":"mcp"}.items():
        if key in manifest and manifest[key] != value:
            raise ValueError("Installed MCP identity or target changed; refusing credential redirection")
    return MCP_SAFE.verify_identity(data, env_get(env_name), host, manifest["client_id"])


def repair_mcp_entry(host,target,manifest,base,env_name,manifest_path,identity_verified=False,dry_run=False):
    with MCP_SAFE.WINDOWS.host_lock(PROFILE_ROOT, host):
        return _repair_mcp_entry(host,target,manifest,base,env_name,manifest_path,identity_verified,dry_run)


def _repair_mcp_entry(host,target,manifest,base,env_name,manifest_path,identity_verified=False,dry_run=False):
    """Called under the host lock; only recognized owned entries may be replaced."""
    if (manifest_path.parent/"mcp-repair.pending.json").exists():
        raise ValueError("Unfinished MCP repair; inspect its local recovery evidence")
    old = MCP_SAFE.raw_file(target)
    old_manifest = MCP_SAFE.raw_file(manifest_path)
    if old_manifest is not None and not MCP_SAFE.strict_equal(json.loads(old_manifest.decode("utf-8-sig")), manifest):
        raise ValueError("MCP manifest changed during repair")
    present, actual = mcp_actual(host, old)
    expected = expected_mcp_entry(host, base, env_name)
    permitted = [expected]
    if manifest.get("owned_entry"):
        permitted.append(manifest["owned_entry"])
    if base == "http://127.0.0.1:8765" and host in {"workbuddy","qoder"}:
        permitted.append({**expected,"args":expected["args"][:-2]})
    if identity_verified and base == "http://127.0.0.1:8765":
        bridge_args = [str(ROOT/"workbuddy_readonly_bridge.py"),"--token-env",env_name]
        if host == "codex":
            permitted.append({"command":str(PYTHON),"args":bridge_args,"startup_timeout_sec":30})
        elif host == "opencode":
            permitted.append({"type":"local","command":[str(PYTHON),*bridge_args],"enabled":True})
    if present and not any(MCP_SAFE.strict_equal(actual, entry) for entry in permitted):
        raise ValueError("Owned MCP entry was edited; preserve the user's changes")
    new = old if present and MCP_SAFE.strict_equal(actual, expected) else mcp_patch(host, old, expected)
    updated = {**manifest,"owned_entry":expected,"installed_config_sha256":hashlib.sha256(new).hexdigest()}
    new_manifest = old_manifest if MCP_SAFE.strict_equal(manifest, updated) and old_manifest is not None else MCP_SAFE.encoded_json(updated)
    if dry_run:
        changed = old != new or old_manifest != new_manifest
        return {"changed":changed,"target_changed":old!=new,"manifest_changed":old_manifest!=new_manifest,
            "idempotent":not changed,"dry_run":True}
    return MCP_SAFE.commit_pair(target,manifest_path,old,old_manifest,new,new_manifest)


def rebuild_mcp_manifest(host, base=None, data=None, confirm=False):
    with MCP_SAFE.WINDOWS.host_lock(PROFILE_ROOT, host):
        return _rebuild_mcp_manifest(host, base, data, confirm)


def _rebuild_mcp_manifest(host, base=None, data=None, confirm=False):
    """Explicit recovery under the host lock; never issue new credentials."""
    base, data = base or BASE, Path(data) if data else DATA
    guard_install_target(base, data)
    if host != "workbuddy":
        raise ValueError("Manifest recovery is limited to WorkBuddy")
    target, manifest_path = _mcp_target(host), INSTALL_ROOT/host/"mcp.json"
    if manifest_path.exists() or (manifest_path.parent/"mcp-repair.pending.json").exists():
        raise ValueError("Manifest exists or repair is unfinished; preserve it")
    env_name = "PERSONAL_MEMORY_MCP_WORKBUDDY_TOKEN"
    identity = MCP_SAFE.verify_identity(data, env_get(env_name), host)
    old = MCP_SAFE.raw_file(target)
    present, _ = mcp_actual(host, old)
    if present:
        raise ValueError("Existing Personal Memory entry is not owned; preserve it")
    expected = expected_mcp_entry(host, base, env_name)
    new = mcp_patch(host, old, expected)
    manifest = {"host":host,"target":str(target),"client_id":identity["client_id"],"env_name":env_name,
        "mode":"library-read-only","purpose":"mcp","base_url":base,"data_marker":str(data/".personal-memory-data"),
        "owned_entry":expected,"previous_config_sha256":hashlib.sha256(old).hexdigest() if old is not None else None,
        "installed_config_sha256":hashlib.sha256(new).hexdigest()}
    result = {"host":host,"client_id":identity["client_id"],"mode":"library-read-only","dry_run":not confirm,
        "target":str(target),"manifest":str(manifest_path)}
    if confirm:
        result.update(MCP_SAFE.commit_pair(target,manifest_path,old,None,new,MCP_SAFE.encoded_json(manifest)))
    else:
        result.update(changed=True,target_changed=True,manifest_changed=True,idempotent=False)
    return result


def install_library_mcp(host, base=None, data=None):
    with MCP_SAFE.WINDOWS.host_lock(PROFILE_ROOT, host):
        return _install_library_mcp(host, base, data)


def _install_library_mcp(host, base=None, data=None):
    base = base or BASE
    data = Path(data) if data else DATA
    guard_install_target(base, data)
    """Issue a separate read-only library token and migrate this host's MCP entry."""
    if host not in HOSTS:
        raise ValueError("Unsupported MCP host")
    env_name = "PERSONAL_MEMORY_MCP_" + host.upper() + "_TOKEN"
    folder = INSTALL_ROOT / host
    manifest_path = folder / "mcp.json"
    if (folder / "mcp-repair.pending.json").exists():
        raise ValueError("Unfinished MCP repair; inspect its local recovery evidence")
    manifest = read(manifest_path)
    target = _mcp_target(host)
    if manifest and env_get(env_name):
        validate_mcp_manifest(host,manifest,target,base,data,env_name)
        result = _repair_mcp_entry(host,target,manifest,base,env_name,manifest_path,identity_verified=True)
        return {"host":host,"already_configured":True,
            "client_id":manifest.get("client_id"),"mode":"library-read-only",**result}
    if manifest or env_get(env_name):
        raise ValueError("Incomplete MCP installation; inspect before replacing")
    old = MCP_SAFE.raw_file(target)
    present, _ = mcp_actual(host, old)
    if present:
        raise ValueError("Existing Personal Memory entry is not owned; preserve it")
    expected = expected_mcp_entry(host, base, env_name)
    merged = mcp_patch(host, old, expected)
    opener, csrf = session(base, data)
    client = None
    token = None
    primary_error = None
    try:
        client = call(opener, base, "/api/automation/library-clients", "POST",
            {"app": host, "excluded_roots": [], "purpose": "mcp"}, csrf,
            {"x-personal-memory-installer": "local-installer"})
        token = client.pop("token")
        env_set(env_name, token)
        installed = {"host":host,"target":str(target),"client_id":client["client_id"],"env_name":env_name,
            "mode":"library-read-only","purpose":"mcp","base_url":base,"data_marker":str(data/".personal-memory-data"),
            "owned_entry":expected,"previous_config_sha256":hashlib.sha256(old).hexdigest() if old is not None else None,
            "installed_config_sha256":hashlib.sha256(merged).hexdigest()}
        result = MCP_SAFE.commit_pair(target,manifest_path,old,None,merged,MCP_SAFE.encoded_json(installed))
        return {"host":host,"client_id":client["client_id"],"mcp_config":str(target),"mode":"library-read-only",**result}
    except Exception as error:
        primary_error = error
        if (manifest_path.parent / "mcp-repair.pending.json").exists():
            error.add_note("MCP transaction unresolved; preserve its client and credential for inspection")
        else:
            try:
                if token is not None and env_get(env_name) == token:
                    env_delete(env_name)
            except Exception:
                error.add_note("MCP credential cleanup failed; inspect the scoped installation")
            try:
                if client:
                    call(opener, base, "/api/automation/clients/" + client["client_id"],
                        "PATCH", {"enabled": False, "revoke": True}, csrf)
            except Exception:
                error.add_note("MCP newly-created client cleanup failed")
        raise
    finally:
        try:
            call(opener, base, "/api/session", "DELETE", csrf=csrf)
        except Exception:
            if primary_error is not None:
                primary_error.add_note("MCP administrative session cleanup failed")
            # Session expiry handles cleanup; never turn a committed install into failure.


def install(host, base=None, data=None, project=None, roots=None, library=True):
    base = base or BASE
    data = Path(data) if data else DATA
    guard_install_target(base, data)
    roots = [Path(p).resolve() for p in (roots or [])]
    if not PYTHON.is_file() or not HOOK.is_file() or not all(p.is_dir() for p in roots):
        raise ValueError("Bridge runtime or configured directory is missing")
    if not library and (not project or not roots):
        raise ValueError("A legacy binding needs a project and roots")
    folder = INSTALL_ROOT / host
    config_path, manifest_path = folder / "config.json", folder / "install.json"
    existing_config = read(config_path)
    existing_manifest = read(manifest_path)
    if (config_path.exists() or manifest_path.exists()) and not (
        library and existing_config.get("access_mode", "bound") in {"bound", "library"}
        and existing_manifest
    ):
        raise ValueError("Automatic adapter already exists; use status or the local UI to manage it")
    target = config_target(host)
    old = target.read_bytes() if target.exists() else None
    if host == "opencode" and old is not None and not existing_manifest:
        raise ValueError("OpenCode automatic plugin already exists; inspect before changing")
    env_name = "PERSONAL_MEMORY_AUTO_" + host.upper() + "_TOKEN"
    if env_get(env_name) and existing_config.get("access_mode", "bound") not in {"bound", "library"}:
        raise ValueError("An automatic credential already exists; inspect before changing")
    if library and existing_config.get("access_mode") == "library" and existing_manifest:
        config = existing_config.copy()
        config.update({"base_url": base})
        if host == "opencode" and old is not None and hashlib.sha256(old).hexdigest()!=existing_manifest.get("installed_sha256"):
            raise ValueError("Owned plugin was edited; preserve the user's changes")
        old_group=existing_manifest.get("group")
        if host != "opencode" and old is not None:
            owned=json.loads(old.decode("utf-8-sig"))
            expected_command=str(HOOK).casefold()
            for event,groups in owned.get("hooks",{}).items():
                for entry in groups:
                    if entry!=old_group and any(expected_command in str(h.get("command","")).casefold() for h in entry.get("hooks",[])):
                        raise ValueError("Owned hook was edited; preserve the user's changes")
            for event in host_hook_events(host):
                owned.setdefault("hooks",{})[event]=[g for g in owned.get("hooks",{}).get(event,[]) if g!=old_group]
            old_for_merge=json.dumps(owned).encode("utf8")
        else:
            old_for_merge=old
        group = hook_group(config_path)
        merged = merge_hooks(json.loads(old_for_merge.decode("utf-8-sig")) if old_for_merge else {},
                             group, host_hook_events(host)) if host != "opencode" else None
        if host == "opencode":
            template = (ROOT / "connect-agent/adapters/opencode.mjs").read_text("utf8")
            for marker, value in (("__PM_PYTHON__", opencode_hook_python()), ("__PM_HOOK__", HOOK), ("__PM_CONFIG__", config_path)):
                template = template.replace(marker, json.dumps(str(value)))
            current_target = template.encode("utf8")
        else:
            current_target = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf8")
        config_changed = read(config_path) != config
        target_changed = old != current_target
        manifest = {**existing_manifest, "group": group,
            "installed_sha256": hashlib.sha256(current_target).hexdigest()}
        manifest_changed = read(manifest_path) != manifest
        if config_changed:
            write(config_path, config)
        if target_changed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current_target)
        if manifest_changed:
            write(manifest_path, manifest)
        return {"host": host, "configuration_saved": True, "automatic_client_id": config["client_id"],
            "access_mode": "library", "idempotent": not (config_changed or target_changed or manifest_changed),
            "real_desktop_chat": "NOT_RUN", "codex_exact_hook_trust": "preserve_existing" if host == "codex" else "not_applicable"}
    if library and existing_config.get("access_mode", "bound") == "bound" and existing_manifest:
        opener, csrf = session(base, data)
        client = None
        saved_env = False
        old_config_path = config_path.read_bytes()
        old_manifest_path = manifest_path.read_bytes()
        old_target = old
        old_env = env_get(env_name)
        try:
            call(opener, base, "/api/backup", "POST", csrf=csrf)
            client = call(opener, base, "/api/automation/library-clients", "POST",
                {"app": host, "excluded_roots": [str(p) for p in roots]}, csrf)
            token_value = client.pop("token")
            spool = data / "automation-outbox" / client["client_id"]
            spool.mkdir(parents=True, exist_ok=True)
            user = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip().split(",")[-1].strip('"')
            if subprocess.run(["icacls", str(spool), "/inheritance:r", "/grant:r",
                    f"*{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                    capture_output=True).returncode:
                raise RuntimeError("Could not restrict automatic outbox permissions")
            env_set(env_name, token_value)
            saved_env = True
            config = {"enabled": True, "app": host, "base_url": base,
                "client_id": client["client_id"], "access_mode": "library",
                "binding_id": None, "token_env": env_name, "roots": [],
                "excluded_roots": [str(p) for p in roots],
                "state_dir": str(folder / "state"), "outbox_dir": str(spool),
                "data_marker": str(data / ".personal-memory-data"),
                "transcript_roots": [str(PROFILE_ROOT / (".workbuddy" if host == "workbuddy" else ".codex"))]
                    if host not in {"opencode", "qoder"} else []}
            group = hook_group(config_path)
            if host == "opencode":
                template = (ROOT / "connect-agent/adapters/opencode.mjs").read_text("utf8")
                for marker, value in (("__PM_PYTHON__", opencode_hook_python()), ("__PM_HOOK__", HOOK), ("__PM_CONFIG__", config_path)):
                    template = template.replace(marker, json.dumps(str(value)))
                new_target = template.encode("utf8")
                merged = None
            else:
                merged = merge_hooks(json.loads(old.decode("utf-8-sig")) if old else {},
                                     group, host_hook_events(host))
                new_target = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf8")
            write(config_path, config)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_target)
            write(manifest_path, {"host": host, "target": str(target), "group": group,
                "client_id": client["client_id"],
                "installed_sha256": hashlib.sha256(new_target).hexdigest(),
                "backup": existing_manifest.get("backup")})
            call(opener, base, "/api/automation/clients/" + existing_config["client_id"],
                "PATCH", {"enabled": False, "revoke": True}, csrf)
            return {"host": host, "configuration_saved": True,
                "automatic_client_id": client["client_id"], "access_mode": "library",
                "upgraded_from": existing_config["client_id"], "idempotent": False,
                "real_desktop_chat": "NOT_RUN",
                "codex_exact_hook_trust": "preserve_existing" if host == "codex" else "not_applicable"}
        except Exception:
            config_path.write_bytes(old_config_path)
            manifest_path.write_bytes(old_manifest_path)
            if old_target is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old_target)
            if old_env is None:
                if saved_env: env_delete(env_name)
            else:
                env_set(env_name, old_env)
            if client:
                call(opener, base, "/api/automation/clients/" + client["client_id"],
                    "PATCH", {"enabled": False, "revoke": True}, csrf)
            raise
        finally:
            call(opener, base, "/api/session", "DELETE", csrf=csrf)
    group = hook_group(config_path)
    merged = merge_hooks(json.loads(old.decode("utf-8-sig")) if old else {},
                         group, host_hook_events(host)) if host != "opencode" else None
    opener, csrf = session(base, data)
    client = None
    saved_env = False
    changed_target = False
    try:
        call(opener, base, "/api/backup", "POST", csrf=csrf)
        client = call(opener, base, "/api/automation/library-clients", "POST", {"app": host, "excluded_roots": [str(p) for p in roots]}, csrf) if library else call(opener, base, "/api/automation/clients", "POST", {"app": host, "project_id": project, "roots": [str(p) for p in roots]}, csrf)
        token_value = client.pop("token")
        folder.mkdir(parents=True, exist_ok=True)
        # Inherited profile ACLs exclude other ordinary Windows users. Tighten
        # this component folder explicitly; same-user processes remain trusted.
        user = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip().split(",")[-1].strip('"')
        result = subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"*{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], capture_output=True)
        if result.returncode:
            raise RuntimeError("Could not restrict adapter folder permissions")
        env_set(env_name, token_value)
        saved_env = True
        spool = data / "automation-outbox" / client["client_id"]
        spool.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["icacls", str(spool), "/inheritance:r", "/grant:r", f"*{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], capture_output=True).returncode:
            raise RuntimeError("Could not restrict automatic outbox permissions")
        config = {"enabled": True, "app": host, "base_url": base, "client_id": client["client_id"],
            "access_mode": client["access_mode"], "binding_id": client["binding_id"],
            "token_env": env_name, "roots": [str(p) for p in roots],
            "excluded_roots": [str(p) for p in roots],
            "state_dir": str(folder / "state"), "outbox_dir": str(spool),
            "data_marker":str(data/".personal-memory-data"),
            "transcript_roots": [str(PROFILE_ROOT / (".workbuddy" if host == "workbuddy" else ".codex"))] if host not in {"opencode", "qoder"} else []}
        write(config_path, config)
        if old is not None:
            backup_path = target.with_name(target.name + ".pre-automatic-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
            if backup_path.exists():
                raise ValueError("Configuration backup already exists")
            backup_path.write_bytes(old)
        else:
            backup_path = None
        if host == "opencode":
            template = (ROOT / "connect-agent/adapters/opencode.mjs").read_text("utf8")
            for key, value in (("__PM_PYTHON__", opencode_hook_python()), ("__PM_HOOK__", HOOK), ("__PM_CONFIG__", config_path)):
                template = template.replace(key, json.dumps(str(value)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(template, encoding="utf8")
        else:
            write(target, merged)
        changed_target = True
        write(manifest_path, {"host": host, "target": str(target), "group": group, "client_id": client["client_id"],
            "installed_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "backup": str(backup_path) if backup_path else None})
        return {"host": host, "configuration_saved": True, "automatic_client_id": client["client_id"],
            "access_mode": client["access_mode"], "real_desktop_chat": "NOT_RUN",
            "codex_exact_hook_trust": "review_required" if host == "codex" else "not_applicable"}
    except Exception:
        if changed_target:
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        config_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        if saved_env:
            env_delete(env_name)
        if client:
            call(opener, base, "/api/automation/clients/" + client["client_id"], "PATCH", {"enabled": False, "revoke": True}, csrf)
        raise
    finally:
        call(opener, base, "/api/session", "DELETE", csrf=csrf)


def disable(host, base="http://127.0.0.1:8765", data=DATA):
    folder = INSTALL_ROOT / host
    cfg_path, manifest_path = folder / "config.json", folder / "install.json"
    cfg, manifest = read(cfg_path), read(manifest_path)
    if not cfg or not manifest:
        raise ValueError("No automatic adapter is installed")
    opener, csrf = session(base, data)
    try:
        call(opener, base, "/api/automation/clients/" + cfg["client_id"], "PATCH", {"enabled": False}, csrf)
        cfg["enabled"] = False
        write(cfg_path, cfg)
        target = Path(manifest["target"])
        if host == "opencode":
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != manifest["installed_sha256"]:
                    raise ValueError("Plugin changed since install; paused without deleting the edited file")
                target.unlink()
        elif target.exists():
            current = read(target)
            for event in host_hook_events(host):
                entries = current.get("hooks", {}).get(event, [])
                current["hooks"][event] = [g for g in entries if g != manifest["group"]]
            write(target, current)
        return {"host": host, "enabled": False, "other_settings_preserved": True}
    finally:
        call(opener, base, "/api/session", "DELETE", csrf=csrf)
