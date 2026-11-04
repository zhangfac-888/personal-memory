"""Bounded native provenance checks; hook names and prompt text are not proof."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

WINDOW = 2 * 1024 * 1024


def sha(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def verify(config, pending, session_id, turn_id):
    """Update a private scan receipt; never return or retain native message text.

    Start with a bounded tail at Submit, then resume that exact file at Stop.
    A late/partial row can be retried. Missing markers remain unknown rather
    than inferring human origin from text or a tool-result's matching promptId.
    """
    receipt = pending.setdefault("source_check", {"version": 1, "state": "unknown"})
    if receipt.get("state") in {"verified", "rejected"}:
        return receipt
    path = pending.get("source_path")
    roots = config.get("transcript_roots", [])
    try:
        source = Path(path).resolve() if path else None
        if source is None or not any(source == Path(r).resolve() or Path(r).resolve() in source.parents for r in roots):
            receipt.update(reason="source_path_unavailable")
            return receipt
        stat = source.stat()
        identity = [stat.st_dev, stat.st_ino]
        if receipt.get("file_identity") not in (None, identity) or receipt.get("cursor", 0) > stat.st_size:
            receipt.update(state="rejected", reason="source_file_changed")
            return receipt
        initial = "cursor" not in receipt
        start = max(0, stat.st_size - WINDOW) if initial else receipt["cursor"]
        with source.open("rb") as stream:
            stream.seek(start)
            data = stream.read(WINDOW)
        if initial and start:
            cut = data.find(b"\n")
            if cut < 0:
                receipt.update(reason="source_row_too_large")
                return receipt
            start += cut + 1
            data = data[cut + 1:]
        end = data.rfind(b"\n") + 1
        receipt.update(file_identity=identity, cursor=start + end)
        if not end and len(data) == WINDOW:
            receipt.update(reason="source_row_too_large")
            return receipt
        position = start
        for line in data[:end].splitlines(keepends=True):
            row_start = position
            position += len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or row.get("sessionId") != session_id or row.get("promptId") != turn_id:
                continue
            message = row.get("message")
            if row.get("type") != "user" or not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list) and content and all(isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str) for x in content):
                text = "\n".join(x["text"] for x in content)
            else:
                # Tool results, attachments and mixed blocks cannot prove this
                # exact plain-text prompt. They must not be reclassified.
                continue
            if sha(text) != pending.get("raw_prompt_sha256"):
                continue
            origin = row.get("origin")
            human = isinstance(origin, dict) and origin.get("kind") == "human"
            if row.get("isMeta") or row.get("isSidechain") or message.get("isMeta") or not human:
                receipt.update(state="rejected" if human or origin else "unknown", reason="source_not_confirmed_human")
                receipt["nonhuman_match"] = True
                continue
            receipt["candidate"] = {"native_uuid": row.get("uuid"), "native_row_sha256": hashlib.sha256(line).hexdigest(),
                "raw_prompt_sha256": sha(text), "row_offset": row_start, "origin_kind": "human"}
        if receipt.get("nonhuman_match"):
            receipt.update(reason="source_not_confirmed_human")
        elif receipt.get("cursor") == stat.st_size and receipt.get("candidate"):
            receipt.update(state="verified", reason="native_human_exact_match", **receipt.pop("candidate"))
        else:
            receipt.update(reason="source_scan_incomplete" if receipt["cursor"] < stat.st_size else "source_not_found")
    except (OSError, ValueError, TypeError):
        receipt.update(reason="source_read_unavailable")
    return receipt
