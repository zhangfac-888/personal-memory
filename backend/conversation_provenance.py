"""Reversible exclusions for confirmed misclassified capture events.

Original event payloads and archive bodies remain untouched. Only an admin
can apply or revoke a hash-bound correction; this grants no reader access.
"""
from __future__ import annotations
import re
from .service import Problem, canonical, digest, now


def visible_sql(alias="a"):
    # Called only with internal SQL aliases, never input from a request.
    return ("NOT EXISTS (SELECT 1 FROM conversation_source_corrections sc "
        "JOIN automation_events se ON se.event_id=sc.event_id "
        f"WHERE sc.event_id={alias}.event_id AND sc.event_content_hash=se.content_hash AND sc.revoked_at IS NULL)")


def _admin(admin):
    if admin.get("kind") != "admin":
        raise Problem("FORBIDDEN", "需要管理权限", 403)


def correct(service, admin, event_id, expected_content_hash, reason, evidence):
    _admin(admin)
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 160 or not isinstance(expected_content_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_content_hash):
        raise Problem("VALIDATION_ERROR", "事件编号或摘要无效", 422)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000 or not isinstance(evidence, dict) or len(canonical(evidence)) > 8000:
        raise Problem("VALIDATION_ERROR", "更正原因或依据无效", 422)
    with service.db.tx() as conn:
        event = conn.execute("SELECT content_hash FROM automation_events WHERE event_id=?", (event_id,)).fetchone()
        if not event:
            raise Problem("NOT_FOUND", "采集事件不存在", 404)
        if event[0] != expected_content_hash:
            raise Problem("CONFLICT", "采集事件摘要已变化", 409)
        ident = "source_" + digest(canonical([event_id, expected_content_hash]))[:32]
        stamp = now()
        conn.execute("INSERT OR IGNORE INTO conversation_source_corrections VALUES (?,?,?,?,?,?,?,NULL)",
            (ident, event_id, expected_content_hash, reason, canonical(evidence), stamp, stamp))
        row = conn.execute("SELECT revoked_at FROM conversation_source_corrections WHERE correction_id=?", (ident,)).fetchone()
        service._log(conn, admin, "conversation_source_correct", ident)
    return {"correction_id": ident, "event_id": event_id, "active": row[0] is None, "original_text_unchanged": True}


def set_active(service, admin, correction_id, active):
    _admin(admin)
    if type(active) is not bool:
        raise Problem("VALIDATION_ERROR", "更正开关无效", 422)
    with service.db.tx() as conn:
        row = conn.execute("SELECT * FROM conversation_source_corrections WHERE correction_id=?", (correction_id,)).fetchone()
        if not row:
            raise Problem("NOT_FOUND", "更正不存在", 404)
        if active:
            event = conn.execute("SELECT content_hash FROM automation_events WHERE event_id=?", (row["event_id"],)).fetchone()
            if not event or event[0] != row["event_content_hash"]:
                raise Problem("CONFLICT", "原始事件已变化", 409)
        stamp = now()
        conn.execute("UPDATE conversation_source_corrections SET revoked_at=?,updated_at=? WHERE correction_id=?", (None if active else stamp, stamp, correction_id))
        service._log(conn, admin, "conversation_source_restore" if active else "conversation_source_revoke", correction_id)
    return {"correction_id": correction_id, "active": active, "original_text_unchanged": True}


def list_corrections(service, admin):
    _admin(admin)
    with service.db.read() as conn:
        rows = conn.execute("SELECT * FROM conversation_source_corrections ORDER BY created_at DESC,correction_id LIMIT 100").fetchall()
    import json
    return {"items": [{"correction_id": r["correction_id"], "event_id": r["event_id"],
        "event_content_hash": r["event_content_hash"], "reason": r["reason"], "evidence": json.loads(r["evidence_json"]),
        "active": r["revoked_at"] is None, "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows], "limit": 100}
