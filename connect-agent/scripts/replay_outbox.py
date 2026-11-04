"""Explicitly replay quarantined automatic-outbox shards.

The drainer intentionally only retries ``*.json``. A failed shard is moved to
``*.failed`` and is never sent again. This tool is the deliberate, dry-run-first
recovery path; it never runs from the service loop.

It reads a client's failed payload, removes exact duplicate message IDs (the
same rule the hook now applies), recomputes the content hash, validates against
the same request model as the server, and posts it with the client's existing
credential. On success the original is moved under ``replayed/`` and a sidecar
records the source hash and response. Nothing is deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.automation import LibraryTurnRequest, TurnRequest  # noqa: E402
from backend.service import canonical, digest  # noqa: E402

DATA = Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")))
BASE = os.environ.get("PERSONAL_MEMORY_BASE", "http://127.0.0.1:8765")
INSTALL_ROOT = Path.home() / ".personal-memory" / "automation"
ENV_STORE = None  # Tests may inject a dict; real runs use the process env or HKCU.


class ReplayError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def read_json(path):
    return json.loads(path.read_text("utf-8-sig"))


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf8")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_config(client_id=None, config_path=None):
    if config_path is not None:
        config = read_json(Path(config_path))
        if client_id and config.get("client_id") != client_id:
            raise ValueError("Config client_id does not match --client-id")
        return config, Path(config_path)
    if not client_id:
        raise ValueError("Use --config or --client-id")
    for path in sorted(INSTALL_ROOT.glob("*/config.json")):
        try:
            config = read_json(path)
        except (OSError, ValueError):
            continue
        if config.get("client_id") == client_id:
            return config, path
    raise ValueError("No installed client config matches --client-id")


def env_token(name):
    if ENV_STORE is not None:
        return ENV_STORE.get(name)
    value = os.environ.get(name)
    if value:
        return value
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except (FileNotFoundError, ImportError, OSError):
        return None


def dedupe_messages(messages):
    """Keep the first exact duplicate ID; refuse conflicting content on one ID."""
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    cleaned, seen, conflicts = [], {}, []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            cleaned.append(message)
            continue
        if message_id in seen:
            if canonical(seen[message_id]) != canonical(message):
                conflicts.append(message_id)
            continue
        seen[message_id] = message
        cleaned.append(message)
    if conflicts:
        raise ValueError("duplicate message ID with different content: " + ", ".join(conflicts[:5]))
    return cleaned, {
        "messages_before": len(messages),
        "messages_after": len(cleaned),
        "duplicates_removed": len(messages) - len(cleaned),
    }


def prepare_payload(raw, access_mode=None):
    if not isinstance(raw, dict):
        raise ValueError("top-level payload must be an object")
    messages, stats = dedupe_messages(raw.get("messages"))
    payload = dict(raw)
    payload["messages"] = messages
    if access_mode == "library":
        model = LibraryTurnRequest
    elif access_mode == "bound":
        model = TurnRequest
    else:
        model = LibraryTurnRequest if "workspace_binding_id" not in raw else TurnRequest
    payload["content_hash"] = "0" * 64
    validated = model.model_validate(payload)
    clean = [message.model_dump(exclude_none=True) for message in validated.messages]
    payload["messages"] = clean
    payload["content_hash"] = digest(canonical(clean))
    model.model_validate(payload)
    stats.update({
        "model": "library" if model is LibraryTurnRequest else "bound",
        "content_hash": payload["content_hash"],
        "turn_id": raw.get("turn_id"),
        "original_turn_id": raw.get("original_turn_id"),
        "turn_part_index": raw.get("turn_part_index"),
        "turn_part_count": raw.get("turn_part_count"),
    })
    return payload, stats


def send_payload(payload, access_mode, base_url, token):
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ReplayError("INVALID_BASE_URL", "Replay must use a local loopback service")
    endpoint = "/automation/v2/turns" if access_mode == "library" else "/automation/v1/turns"
    request = urllib.request.Request(
        base_url.rstrip("/") + endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf8"))
        except (ValueError, OSError):
            detail = {}
        raise ReplayError(detail.get("code", "HTTP_" + str(error.code)),
            detail.get("message", "Replay request failed"), error.code)
    except urllib.error.URLError as error:
        raise ReplayError("SERVICE_UNAVAILABLE", str(error.reason))


def replay_one(path, config, token, base_url, access_mode=None, dry_run=True,
        move=True, force=False, sender=send_payload):
    path = Path(path)
    if path.suffix != ".failed":
        raise ValueError("Only .failed shards can be replayed")
    outbox = Path(config.get("outbox_dir") or path.parent)
    replayed = outbox / "replayed"
    sidecar = replayed / (path.name + ".replayed.json")
    if sidecar.exists() and not force:
        return {"file": path.name, "status": "already_replayed", "sidecar": str(sidecar)}
    source_hash = sha256(path)
    raw = read_json(path)
    payload, stats = prepare_payload(raw, access_mode or config.get("access_mode"))
    result = {"file": path.name, "status": "would_replay" if dry_run else "replayed",
        "source_sha256": source_hash, **stats}
    if dry_run:
        return result
    response = sender(payload, stats["model"], base_url, token)
    record = {
        "at": datetime.now().astimezone().isoformat(),
        "status": "replayed",
        "client_id": config.get("client_id"),
        "source_file": path.name,
        "source_sha256": source_hash,
        "content_hash": stats["content_hash"],
        "turn_id": stats["turn_id"],
        "original_turn_id": stats["original_turn_id"],
        "turn_part_index": stats["turn_part_index"],
        "turn_part_count": stats["turn_part_count"],
        "messages_before": stats["messages_before"],
        "messages_after": stats["messages_after"],
        "duplicates_removed": stats["duplicates_removed"],
        "response": response,
    }
    atomic_write(sidecar, record)
    result["response"] = response
    result["sidecar"] = str(sidecar)
    if move:
        destination = replayed / path.name
        os.replace(path, destination)
        result["moved_to"] = str(destination)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicitly replay quarantined Personal Memory outbox shards")
    parser.add_argument("--client-id")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--outbox-dir", type=Path)
    parser.add_argument("--token-env")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--access-mode", choices=("library", "bound"))
    parser.add_argument("--file", action="append", type=Path)
    parser.add_argument("--all-failed", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--no-move", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.file and not args.all_failed:
        parser.error("use --file or --all-failed")
    try:
        config, config_path = find_config(args.client_id, args.config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.outbox_dir:
        config = {**config, "outbox_dir": str(args.outbox_dir)}
    if not config.get("outbox_dir"):
        if not config.get("client_id"):
            parser.error("outbox_dir is unavailable without a client_id")
        config = {**config, "outbox_dir": str(DATA / "automation-outbox" / config["client_id"])}
    if args.base_url:
        config = {**config, "base_url": args.base_url}
    base_url = config.get("base_url") or BASE
    token = None
    if args.confirm:
        env_name = args.token_env or config.get("token_env")
        if not env_name:
            parser.error("no token environment name is available")
        token = env_token(env_name)
        if not token:
            parser.error("credential is missing from the environment")
    if args.all_failed:
        paths = sorted(Path(config["outbox_dir"]).glob("*.failed"),
            key=lambda path: path.stat().st_mtime)[:max(0, args.limit)]
    else:
        paths = []
        for path in args.file:
            paths.append(path if path.is_absolute() else Path(config["outbox_dir"]) / path)
    results = []
    for path in paths:
        try:
            result = replay_one(path, config, token, base_url, args.access_mode,
                dry_run=not args.confirm, move=not args.no_move,
                force=args.force)
        except (OSError, ValueError, ReplayError) as error:
            result = {"file": Path(path).name, "status": "failed",
                "code": getattr(error, "code", type(error).__name__),
                "message": getattr(error, "message", str(error))}
        results.append(result)
    if args.json:
        print(json.dumps({"dry_run": not args.confirm, "config": str(config_path),
            "results": results}, ensure_ascii=False, indent=2))
    else:
        print(("DRY RUN" if not args.confirm else "REPLAY") + f" client={config.get('client_id')}")
        for result in results:
            detail = f"{result['file']} {result['status']}"
            if result.get("messages_before") is not None:
                detail += f" messages={result['messages_before']}->{result['messages_after']}"
            if result.get("duplicates_removed"):
                detail += f" duplicates={result['duplicates_removed']}"
            if result.get("code"):
                detail += f" code={result['code']} message={result.get('message')}"
            print(detail)
    return 1 if any(result["status"] == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
