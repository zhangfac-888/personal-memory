"""One-step, scoped Personal Memory connection for the DeepSeek Harness (DSH) host.

Creates a dedicated read-only, project-scoped client, persists its bearer token to
the user-level environment (PERSONAL_MEMORY_DSH_TOKEN) and to the harness home
`.env`, then proves the MCP endpoint over Streamable HTTP with a real search and
a negative read-only check.

The script never prints a token or any memory text.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import http.cookiejar
import json
import os
from pathlib import Path
import urllib.request
import winreg


DATA_DIR = Path(str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data"))
BASE = "http://127.0.0.1:8765"
MCP_URL = BASE + "/mcp/"
PROJECT_ID = os.environ.get("PERSONAL_MEMORY_PROJECT_ID", "global")
PROJECT_NAME = os.environ.get("PERSONAL_MEMORY_PROJECT_NAME", "Global")
CLIENT_NAME = "DSH read-only"
ENV_NAME = "PERSONAL_MEMORY_DSH_TOKEN"
DSH_ENV_FILE = Path.home() / ".dsh" / ".env"


def call(opener, path: str, method: str = "GET", payload=None, csrf=None):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["x-csrf-token"] = csrf
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with opener.open(request, timeout=20) as response:
        return json.load(response)


def login():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    admin_key = (DATA_DIR / "admin-key.txt").read_text(encoding="ascii").strip()
    csrf = call(opener, "/api/session", "POST", {"key": admin_key})["csrf_token"]
    return opener, csrf


def env_token() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, ENV_NAME)
            return value or None
    except OSError:
        return None


def active_client(clients: list[dict]) -> dict | None:
    for client in clients:
        if client["name"] == CLIENT_NAME and not client["revoked"]:
            return client
    return None


def persist_env(token: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        winreg.SetValueEx(key, ENV_NAME, 0, winreg.REG_SZ, token)
    # Tell already-running shells/Explorer to refresh their inherited block.
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
    )


def persist_dsh_env(token: str) -> None:
    lines: list[str] = []
    if DSH_ENV_FILE.exists():
        lines = DSH_ENV_FILE.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith(ENV_NAME + "=")]
    kept.append(f"{ENV_NAME}={token}")
    DSH_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    DSH_ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")


async def verify_mcp(token: str) -> dict:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        follow_redirects=False,
    )
    try:
        async with streamable_http_client(MCP_URL, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                result = await session.call_tool(
                    "search_memories",
                    {
                        "query": "项目",
                        "project_id": PROJECT_ID,
                        "include_global": False,
                        "limit": 3,
                    },
                )
                if result.is_error:
                    raise RuntimeError("read-only memory search failed")
                payload = json.loads(result.content[0].text)
                items = payload.get("items", [])
                write_denied = False
                try:
                    denied = await session.call_tool(
                        "save_memory",
                        {
                            "project_id": PROJECT_ID,
                            "content": "scope probe",
                            "type": "Fact",
                            "confidence": "Reported",
                            "source_content": "scope probe",
                            "source_title": "scope probe",
                            "idempotency_key": "dsh-scope-probe-20261024",
                        },
                    )
                    write_denied = bool(denied.is_error)
                except Exception:
                    write_denied = True
                return {
                    "tools": tools,
                    "matches": len(items),
                    "write_denied": write_denied,
                }
    finally:
        await client.aclose()


def status() -> None:
    try:
        health = call(urllib.request.build_opener(), "/api/health")
        service_ok = health.get("status") == "ok"
    except Exception:
        service_ok = False
    report = {
        "service_ok": service_ok,
        "host": "deepseek-harness",
        "env_token_present": bool(env_token()),
        "dsh_env_present": DSH_ENV_FILE.exists()
        and any(line.startswith(ENV_NAME + "=") for line in DSH_ENV_FILE.read_text(encoding="utf-8").splitlines()),
    }
    if service_ok:
        opener, csrf = login()
        try:
            clients = call(opener, "/api/clients")
            entry = active_client(clients)
            report["client"] = (
                {
                    "client_id": entry["client_id"],
                    "project_ids": entry["project_ids"],
                    "can_write": entry["can_write"],
                }
                if entry
                else None
            )
        finally:
            call(opener, "/api/session", "DELETE", csrf=csrf)
    print(json.dumps(report, ensure_ascii=False))


def install(rotate: bool) -> None:
    opener, csrf = login()
    client_id = None
    env_saved = False
    try:
        projects = call(opener, "/api/projects")
        if not any(p["project_id"] == PROJECT_ID for p in projects):
            raise RuntimeError("The selected project does not exist")

        clients = call(opener, "/api/clients")
        existing = active_client(clients)
        if existing and not rotate:
            raise RuntimeError("An active DSH client exists; rerun with --rotate to replace it")

        backup = call(opener, "/api/backup", "POST", csrf=csrf)

        created = call(
            opener,
            "/api/clients",
            "POST",
            {"name": CLIENT_NAME, "project_ids": [PROJECT_ID], "can_write": False},
            csrf=csrf,
        )
        client_id = created["client_id"]
        token = created["token"]

        persist_env(token)
        env_saved = True
        persist_dsh_env(token)

        if existing:
            call(opener, "/api/clients/" + existing["client_id"] + "/revoke", "POST", csrf=csrf)

        check = asyncio.run(verify_mcp(token))
        print(json.dumps({
            "status": "installed_and_protocol_verified",
            "host": "deepseek-harness",
            "client_id": client_id,
            "project_id": PROJECT_ID,
            "can_write": False,
            "backup_id": backup["backup_id"],
            "revoked_previous": bool(existing),
            **check,
            "dsh_chat_test": "not_run",
        }, ensure_ascii=False))
    except Exception:
        if env_saved:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, ENV_NAME)
            except OSError:
                pass
        if client_id:
            call(opener, "/api/clients/" + client_id + "/revoke", "POST", csrf=csrf)
        raise
    finally:
        call(opener, "/api/session", "DELETE", csrf=csrf)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Memory connector for the DeepSeek Harness")
    parser.add_argument("command", choices=("status", "install"))
    parser.add_argument("--rotate", action="store_true", help="replace an existing DSH read-only client")
    args = parser.parse_args()
    if args.command == "status":
        status()
    else:
        install(args.rotate)


if __name__ == "__main__":
    main()
