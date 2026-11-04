"""Strict MCP ownership checks, lossless config patches and recoverable writes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path


_lock_spec = importlib.util.spec_from_file_location("personal_memory_windows_lock", Path(__file__).with_name("windows_mcp_lock.py"))
WINDOWS = importlib.util.module_from_spec(_lock_spec)
_lock_spec.loader.exec_module(WINDOWS)


def strict_equal(left, right):
    """Configuration identity includes concrete types, not Python coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def raw_file(path):
    return path.read_bytes() if path.exists() else None


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def encoded_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf8")


class ConfigJSON:
    """Parse JSON/JSONC and retain value spans; reject duplicate keys."""
    def __init__(self, text, comments=False):
        self.text, self.comments, self.pos = text, comments, 0
        self.spans = {}
        self.objects = {}
        self.value = self.parse((), 0)
        self.skip()
        if self.pos != len(text):
            raise ValueError("Unexpected configuration suffix")
        if not isinstance(self.value, dict):
            raise ValueError("MCP configuration root must be an object")

    def skip(self):
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.comments and self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            elif self.comments and self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    raise ValueError("Unclosed JSONC comment")
                self.pos = end + 2
            else:
                break

    def take(self, char):
        self.skip()
        if self.pos >= len(self.text) or self.text[self.pos] != char:
            raise ValueError("Invalid MCP configuration syntax")
        self.pos += 1

    def parse(self, path, depth):
        if depth > 64:
            raise ValueError("MCP configuration nesting exceeds limit")
        self.skip()
        start = self.pos
        if start >= len(self.text):
            raise ValueError("Incomplete MCP configuration")
        char = self.text[start]
        if char == "{":
            self.pos += 1
            value, trailing, last_end = {}, False, self.pos
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] != "}":
                while True:
                    self.skip()
                    if self.pos >= len(self.text) or self.text[self.pos] != '"':
                        raise ValueError("Configuration keys must be quoted")
                    key, end = json.JSONDecoder().raw_decode(self.text, self.pos)
                    self.pos = end
                    if key in value:
                        raise ValueError("Duplicate configuration key")
                    self.take(":")
                    value[key] = self.parse(path + (key,), depth + 1)
                    last_end = self.pos
                    self.skip()
                    if self.pos < len(self.text) and self.text[self.pos] == ",":
                        self.pos += 1
                        self.skip()
                        if self.pos < len(self.text) and self.text[self.pos] == "}":
                            if not self.comments:
                                raise ValueError("Trailing comma in JSON")
                            trailing = True
                            break
                    else:
                        break
            self.skip()
            close = self.pos
            self.take("}")
            self.objects[path] = (start, close, last_end, trailing, len(value))
        elif char == "[":
            self.pos += 1
            value = []
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] != "]":
                while True:
                    value.append(self.parse(path + (len(value),), depth + 1))
                    self.skip()
                    if self.pos < len(self.text) and self.text[self.pos] == ",":
                        self.pos += 1
                        self.skip()
                        if self.pos < len(self.text) and self.text[self.pos] == "]":
                            if not self.comments:
                                raise ValueError("Trailing comma in JSON")
                            break
                    else:
                        break
            self.take("]")
        else:
            value, end = json.JSONDecoder().raw_decode(self.text, self.pos)
            if isinstance(value, float) and not __import__("math").isfinite(value):
                raise ValueError("Non-finite configuration value")
            self.pos = end
        self.spans[path] = (start, self.pos)
        return value

    def put(self, parent, key, value):
        if parent not in self.objects:
            raise ValueError("MCP configuration section must be an object")
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
        path = parent + (key,)
        if path in self.spans:
            start, end = self.spans[path]
            return self.text[:start] + rendered + self.text[end:]
        _, close, last_end, trailing, count = self.objects[parent]
        newline = "\r\n" if "\r\n" in self.text else "\n"
        insertion = newline + json.dumps(key) + ": " + rendered + newline
        text = self.text
        if count and not trailing:
            text = text[:last_end] + "," + text[last_end:]
            close += 1
        return text[:close] + insertion + text[close:]


def json_entry(raw, section, comments=False):
    text = (raw or b"{}").decode("utf-8-sig")
    document = ConfigJSON(text, comments)
    if section in document.value and not isinstance(document.value[section], dict):
        raise ValueError("MCP configuration section must be an object")
    entries = document.value.get(section, {})
    return "personal_memory" in entries, entries.get("personal_memory")


def patch_json(raw, section, expected, comments=False):
    text = (raw or b"{}\n").decode("utf-8-sig")
    document = ConfigJSON(text, comments)
    if section in document.value:
        updated = document.put((section,), "personal_memory", expected)
    else:
        updated = document.put((), section, {"personal_memory": expected})
    parsed = ConfigJSON(updated, comments).value
    original = document.value
    original.setdefault(section, {})["personal_memory"] = expected
    if not strict_equal(parsed, original):
        raise ValueError("MCP patch changed unrelated configuration")
    return (b"\xef\xbb\xbf" if raw and raw.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf8")


def toml_bounds(text, name):
    """Only edit a contiguous plain table family; ambiguity is rejected."""
    headers = list(re.finditer(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:#[^\r\n]*)?", text))
    members = [i for i, match in enumerate(headers)
               if match[1].strip() == name or match[1].strip().startswith(name + ".")]
    if not members:
        return None
    if headers[members[0]][1].strip() != name or members != list(range(members[0], members[-1] + 1)):
        raise ValueError("Unsupported or non-contiguous MCP TOML table family")
    end = headers[members[-1] + 1].start() if members[-1] + 1 < len(headers) else len(text)
    start = headers[members[0]].start()
    # Keep the trivia preceding the next table (or EOF), not just parsed values.
    for line in reversed(text[start:end].splitlines(keepends=True)):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        end -= len(line)
    return start, end


def patch_toml(raw, expected):
    import tomllib
    text = (raw or b"").decode("utf-8-sig")
    original = tomllib.loads(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = "[mcp_servers.personal_memory]" + newline
    replacement += "".join(key + " = " + json.dumps(value) + newline for key, value in expected.items())
    bounds = toml_bounds(text, "mcp_servers.personal_memory")
    entries = original.get("mcp_servers", {})
    if not isinstance(entries, dict):
        raise ValueError("MCP TOML section must be a table")
    if "personal_memory" in entries and bounds is None:
        raise ValueError("Unsupported MCP TOML table header")
    updated = text[:bounds[0]] + replacement + text[bounds[1]:] if bounds else text + newline + replacement
    original.setdefault("mcp_servers", {})["personal_memory"] = expected
    if not strict_equal(tomllib.loads(updated), original):
        raise ValueError("MCP patch changed unrelated TOML configuration")
    return (b"\xef\xbb\xbf" if raw and raw.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf8")


def verify_identity(data, token, host, client_id=None):
    if not isinstance(token, str) or not token:
        raise ValueError("MCP credential is unavailable")
    database = Path(data) / "memory.db"
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT client_id,app,purpose,access_mode,enabled,revoked FROM automation_clients WHERE token_hash=?",
                (hashlib.sha256(token.encode("utf8")).hexdigest(),)).fetchall()
    except sqlite3.Error:
        raise ValueError("MCP client identity cannot be verified") from None
    if len(rows) != 1:
        raise ValueError("MCP credential identity is not unique or registered")
    row = dict(rows[0])
    if (row["app"] != host or row["purpose"] != "mcp" or row["access_mode"] != "library"
            or not row["enabled"] or row["revoked"] or (client_id is not None and row["client_id"] != client_id)):
        raise ValueError("MCP credential identity does not match the installation")
    return row


def commit_pair(target, manifest_path, old_target, old_manifest, new_target, new_manifest):
    """Exclusive in-place transaction; a crash requires pending-evidence review."""
    pending = manifest_path.parent / "mcp-repair.pending.json"
    if pending.exists():
        raise ValueError("Unfinished MCP repair; inspect its local recovery evidence")
    target_changed, manifest_changed = old_target != new_target, old_manifest != new_manifest
    if not target_changed and not manifest_changed:
        return {"changed": False, "target_changed": False, "manifest_changed": False, "idempotent": True}
    if target.resolve() == manifest_path.resolve():
        raise ValueError("MCP target and manifest must be distinct")
    folder = manifest_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    backup = folder / "backups" / ("mcp-repair-" + identifier)
    backup.mkdir(parents=True)
    if old_target is not None:
        atomic_bytes(backup / "target.before", old_target)
    if old_manifest is not None:
        atomic_bytes(backup / "manifest.before", old_manifest)
    def hashed(value):
        return hashlib.sha256(value).hexdigest() if value is not None else None
    record = {"target": str(target), "manifest": str(manifest_path), "backup": str(backup),
        "target_before": hashed(old_target), "target_after": hashed(new_target),
        "manifest_before": hashed(old_manifest), "manifest_after": hashed(new_manifest),
        "write_mode": "windows-exclusive-in-place", "attempted": [], "state": "pending"}
    with pending.open("xb") as stream:
        stream.write(encoded_json(record))
        stream.flush()
        os.fsync(stream.fileno())
    handles, attempted = {}, set()
    members = ((target, old_target, new_target), (manifest_path, old_manifest, new_manifest))
    try:
        for path, before, _ in sorted(members, key=lambda member: os.path.normcase(str(member[0].resolve()))):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = WINDOWS.ExclusiveFile(path, before)
            handles[path] = handle  # Track an absent-name reservation before validation.
            handle.validate(before)
        if handles[target].identity == handles[manifest_path].identity:
            raise ValueError("MCP target and manifest alias the same file")
        for path, before, after in members:
            if before == after:
                continue
            record["attempted"].append(str(path))
            atomic_bytes(pending, encoded_json(record))
            attempted.add(path)  # Partial writes must also be rolled back.
            handles[path].write_bytes(after)
        record["state"] = "completed"
        atomic_bytes(backup / "transaction.json", encoded_json(record))
    except Exception as error:
        conflict = False
        for path, before, _ in reversed(members):
            handle = handles.get(path)
            if handle is None:
                continue
            try:
                if handle.created:
                    handle.delete_created()
                elif path in attempted:
                    handle.write_bytes(before)
            except Exception:
                conflict = True
        for handle in reversed(list(handles.values())):
            try:
                handle.close()
            except Exception:
                conflict = True
        handles.clear()
        record["state"] = "rollback_conflict" if conflict else "rolled_back"
        try:
            atomic_bytes(backup / "transaction.json", encoded_json(record))
            if not conflict:
                pending.unlink()
        except OSError:
            conflict = True
        if conflict:
            error.add_note("MCP rollback is unresolved; inspect mcp-repair.pending.json")
        raise
    finally:
        for handle in reversed(list(handles.values())):
            handle.close()
    # Failure to clear evidence leaves a pending transaction and never revokes credentials.
    pending.unlink()
    return {"changed": True, "target_changed": target_changed, "manifest_changed": manifest_changed,
        "idempotent": False, "backup": str(backup)}
