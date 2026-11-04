"""One-step, scoped Personal Memory connection for supported local AI clients.

The script never prints tokens or memory text. `install` changes only the
requested client's user config and creates one read-only project credential.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import urllib.request
import winreg


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BRIDGE = ROOT / "workbuddy_readonly_bridge.py"
DATA_DIR = Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")))
BASE = os.environ.get("PERSONAL_MEMORY_BASE", "http://127.0.0.1:8765")
PROJECT_ID = os.environ.get("PERSONAL_MEMORY_PROJECT_ID", "global")
QODER_CONFIG = Path.home() / ".qoder-cn" / "mcp.json"
QODER_ENV = "PERSONAL_MEMORY_QODER_TOKEN"


def qoder_entry() -> dict:
    return {
        "type": "stdio",
        "command": str(PYTHON),
        "args": [str(BRIDGE), "--token-env", QODER_ENV],
    }


def build_qoder_config(existing: dict) -> dict:
    if not isinstance(existing, dict):
        raise ValueError("Qoder settings must be a JSON object")
    config = json.loads(json.dumps(existing))
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Qoder mcpServers must be a JSON object")
    if "personal_memory" in servers:
        raise ValueError("Qoder personal_memory already exists; preserve and inspect it")
    servers["personal_memory"] = qoder_entry()
    return config


def read_qoder_config() -> dict:
    if not QODER_CONFIG.exists():
        return {}
    return json.loads(QODER_CONFIG.read_text(encoding="utf-8-sig"))


def token_present() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, QODER_ENV)
            return bool(value)
    except OSError:
        return False


def call(opener, path: str, method: str = "GET", payload=None, csrf=None):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["x-csrf-token"] = csrf
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with opener.open(request, timeout=15) as response:
        return json.load(response)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


async def verify_bridge() -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    environment = dict(os.environ)
    environment.pop(QODER_ENV, None)  # Prove the persisted user value works.
    parameters = StdioServerParameters(
        command=str(PYTHON),
        args=[str(BRIDGE), "--token-env", QODER_ENV],
        env=environment,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            names = sorted(tool.name for tool in (await session.list_tools()).tools)
            if names != ["get_memory", "search_memories"]:
                raise RuntimeError("Unexpected bridge tools")
            result = await session.call_tool("search_memories", {"query": "项目"})
            if result.is_error:
                raise RuntimeError("Read-only memory search failed")
            payload = json.loads(result.content[0].text)
            return {"tools": names, "matches": len(payload.get("items", []))}


def status() -> None:
    try:
        health = call(urllib.request.build_opener(), "/api/health")
        service_ok = health.get("status") == "ok"
    except Exception:
        service_ok = False
    try:
        entry = read_qoder_config().get("mcpServers", {}).get("personal_memory")
        config_state = "ours" if entry == qoder_entry() else "other" if entry else "missing"
    except (OSError, ValueError, TypeError):
        config_state = "unreadable"
    print(json.dumps({
        "service_ok": service_ok,
        "host": "qoder",
        "config": config_state,
        "token_present": token_present(),
    }))


def install_qoder() -> None:
    if not PYTHON.is_file() or not BRIDGE.is_file():
        raise RuntimeError("Personal Memory bridge runtime is missing")
    old_bytes = QODER_CONFIG.read_bytes() if QODER_CONFIG.exists() else None
    existing = json.loads(old_bytes.decode("utf-8-sig")) if old_bytes else {}
    new_config = build_qoder_config(existing)
    config_backup = None
    if old_bytes is not None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        config_backup = QODER_CONFIG.with_name(f"settings.json.pre-personal-memory-{stamp}.bak")
        if config_backup.exists():
            raise RuntimeError("Qoder config backup name already exists")
    if token_present():
        raise RuntimeError("Qoder token already exists; inspect existing setup before changing it")

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    admin_key = (DATA_DIR / "admin-key.txt").read_text(encoding="ascii").strip()
    csrf = call(opener, "/api/session", "POST", {"key": admin_key})["csrf_token"]
    client_id = None
    token_saved = False
    config_saved = False
    try:
        projects = call(opener, "/api/projects")
        if not any(p["project_id"] == PROJECT_ID for p in projects):
            raise RuntimeError("The selected project does not exist")
        clients = call(opener, "/api/clients")
        if any(c["name"] == "Qoder read-only" and not c["revoked"] for c in clients):
            raise RuntimeError("An active Qoder client exists; inspect before creating another")
        backup = call(opener, "/api/backup", "POST", csrf=csrf)
        client = call(opener, "/api/clients", "POST", {
            "name": "Qoder read-only", "project_ids": [PROJECT_ID], "can_write": False,
        }, csrf=csrf)
        client_id = client["client_id"]
        token = client["token"]

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, QODER_ENV, 0, winreg.REG_SZ, token)
        token_saved = True

        if config_backup is not None:
            atomic_write(config_backup, old_bytes)
        new_bytes = (json.dumps(new_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write(QODER_CONFIG, new_bytes)
        config_saved = True
        check = asyncio.run(verify_bridge())
        print(json.dumps({
            "status": "installed_and_protocol_verified",
            "host": "qoder",
            "client_id": client_id,
            "project_id": PROJECT_ID,
            "can_write": False,
            "backup_id": backup["backup_id"],
            "config_backup": str(config_backup) if config_backup else None,
            **check,
            "qoder_chat_test": "not_run",
        }))
    except Exception:
        if config_saved:
            if old_bytes is None:
                QODER_CONFIG.unlink(missing_ok=True)
            else:
                atomic_write(QODER_CONFIG, old_bytes)
        if token_saved:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, QODER_ENV)
        if client_id:
            call(opener, "/api/clients/" + client_id + "/revoke", "POST", csrf=csrf)
        raise
    finally:
        call(opener, "/api/session", "DELETE", csrf=csrf)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Memory app connector")
    parser.add_argument("command", choices=("status", "detect", "install", "disable", "mcp", "setup-personal"))
    parser.add_argument("--host", choices=("qoder","codex","workbuddy","opencode","all"), default="all")
    parser.add_argument("--rebuild-manifest", action="store_true", help="Dry-run WorkBuddy MCP manifest recovery using its existing credential")
    parser.add_argument("--confirm", action="store_true", help="Commit the explicit manifest recovery")
    args = parser.parse_args()

    import automatic_install as automatic

    if args.rebuild_manifest:
        if args.command != "mcp" or args.host != "workbuddy":
            parser.error("Manifest recovery requires mcp --host workbuddy")
        print(json.dumps(automatic.rebuild_mcp_manifest("workbuddy", BASE, DATA_DIR, confirm=args.confirm), ensure_ascii=False))
        return
    if args.confirm:
        parser.error("--confirm requires --rebuild-manifest")

    if args.command == "setup-personal":
        # Component-owned entry point: auto-discovers and repairs known verified hosts
        results = automatic.discover_and_repair(BASE, DATA_DIR)
        print(json.dumps({"setup_personal": results}, ensure_ascii=False, indent=2))
        if any(r.get("state") in {"repair_failed", "configuration_conflict"} for r in results):
            raise SystemExit(1)
        return

    if args.host != "qoder":
        if args.command == "mcp":
            if args.host == "all":
                parser.error("Install MCP one verified host at a time")
            print(json.dumps(automatic.install_library_mcp(args.host, BASE, DATA_DIR)))
            return
        if args.command in ("status","detect"):
            print(json.dumps([automatic.detect(h) for h in automatic.HOSTS] if args.host=="all" else automatic.detect(args.host), ensure_ascii=False, indent=2))
        elif args.command=="install":
            if args.host == "all":
                results = {}
                for h in automatic.HOSTS:
                    try:
                        inst = automatic.install(h, BASE, DATA_DIR)
                        if inst.get("configuration_saved"):
                            automatic.install_library_mcp(h, BASE, DATA_DIR)
                        results[h] = {"status": "SUCCESS", "details": inst}
                    except Exception as e:
                        results[h] = {"status": "FAIL", "error": type(e).__name__, "message": str(e)}
                print(json.dumps(results, ensure_ascii=False, indent=2))
                if any(r["status"] == "FAIL" for r in results.values()):
                    raise SystemExit(1)
            else:
                print(json.dumps(automatic.install(args.host, BASE, DATA_DIR)))
        elif args.command=="disable":
            if args.host == "all":
                parser.error("Disable one verified host at a time")
            print(json.dumps(automatic.disable(args.host)))
        return

    # Explicit qoder read-only legacy path
    if args.command == "status":
        status()
    elif args.command=="install":
        install_qoder()
    else:
        parser.error("Qoder supports status/install for its existing read-only adapter")


if __name__ == "__main__":
    main()
