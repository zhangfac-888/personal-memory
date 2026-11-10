"""Explicitly granted access to captured conversation text, never instructions."""
from __future__ import annotations

from .service import Problem, QUERY_STOPWORDS, canonical, digest, now, tokenize, uid
from .workspaces import current_client, effective_exclusions, excluded_projects
from .conversation_provenance import visible_sql


def grant(service, admin, client_id, allowed):
    if admin.get("kind") != "admin":
        raise Problem("FORBIDDEN", "需要管理权限", 403)
    if type(allowed) is not bool:
        raise Problem("VALIDATION_ERROR", "授权状态无效", 422)
    with service.db.tx() as conn:
        if conn.execute("UPDATE automation_clients SET archive_read=? WHERE client_id=? AND access_mode='library' AND revoked=0", (int(allowed), client_id)).rowcount != 1:
            raise Problem("NOT_FOUND", "客户端不可用", 404)
        service._log(conn, admin, "archive_read_grant" if allowed else "archive_read_revoke", client_id)
    return {"client_id": client_id, "archive_read": allowed}


def _gate(conn, client):
    client = current_client(conn, {"client_id": client["client_id"]})
    if client["access_mode"] != "library" or not client["archive_read"]:
        raise Problem("FORBIDDEN", "此客户端没有会话读取授权", 403)
    roots = effective_exclusions(conn, client)
    projects = sorted(excluded_projects(conn, client))
    # All policy predicates are inside SQL, before rank and LIMIT. Unknown
    # provenance remains unavailable even when no exclusion is configured.
    sql = ("a.scope_known=1 AND p.status='active' "
           "AND a.project_id NOT IN (SELECT value FROM json_each(?)) "
           "AND NOT EXISTS (SELECT 1 FROM json_each(?) x WHERE a.origin_root=x.value OR substr(a.origin_root,1,length(x.value)+1)=x.value||'/')")
    return sql + " AND " + visible_sql(), [canonical(projects), canonical(roots)]


def _query_terms(query, significant_only, *, strip_dates=True):
    # "11-07 20:48" is a coordinate, not a word: its bare calendar numbers
    # match unrelated text and outrank the subject. Strip the literal before
    # tokenizing, so domain numbers ("Schema 16", "422", "8765") survive.
    # The regex lives in automation_text; a lazy import keeps module loading
    # acyclic and gives both modules one definition instead of two.
    source = query or ""
    if strip_dates:
        from .automation_text import _DATE_TIME_CUE
        source = _DATE_TIME_CUE.sub(" ", source)
    terms = [x for x in dict.fromkeys(tokenize(source)) if x not in QUERY_STOPWORDS][:20]
    if significant_only:
        generic = {"项目", "工作", "问题", "什么", "如何", "这个", "之前", "历史", "会话", "消息", "请问", "记录", "内容", "结果", "用户", "我们"}
        terms = [x for x in terms if (len(x) >= 2 or x.isdigit()) and x not in generic]
    return terms


def search(service, client, query, limit=5, *, exclude_session=None, time_budget_ms=None, significant_only=False, project_ids=None, time_window=None):
    if not isinstance(query, str) or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 50:
        raise Problem("VALIDATION_ERROR", "检索条件无效", 422)
    if project_ids is not None and (not isinstance(project_ids, list) or not 1 <= len(project_ids) <= 20
            or any(not isinstance(pid, str) or not 1 <= len(pid) <= 160 for pid in project_ids)):
        raise Problem("VALIDATION_ERROR", "会话项目范围无效", 422)
    if time_window is not None and (not isinstance(time_window, (list, tuple)) or len(time_window) != 2
            or any(not isinstance(x, str) or not x for x in time_window)
            or time_window[0] > time_window[1]):
        raise Problem("VALIDATION_ERROR", "会话时间范围无效", 422)
    terms = _query_terms(query, significant_only)
    if not terms and not time_window:
        # Without a window, a query that is only a date still has to search
        # literally; stripping it would turn a lookup into an empty result.
        terms = _query_terms(query, significant_only, strip_dates=False)
    with service.db.read() as conn:
        gate, args = _gate(conn, client)
        # Intersect with the authorization gate before ranking and LIMIT.
        if project_ids is not None:
            gate += " AND a.project_id IN (SELECT value FROM json_each(?))"
            args.append(canonical(list(dict.fromkeys(project_ids))))
        if not terms and not time_window:
            return {"items": [], "query_terms": []}
        if time_budget_ms is not None:
            import time
            conn.execute("PRAGMA busy_timeout=100")
            deadline = time.monotonic() + time_budget_ms / 1000
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if exclude_session:
            gate += " AND a.session_id!=?"
            args.append(exclude_session)
        # A time anchor is a coordinate, not a word: parts captured inside the
        # window sort first, and everything else keeps its normal rank. The
        # predicate stays inside the same authorization gate.
        window_order, window_args = "", []
        if time_window:
            window_order = "CASE WHEN a.captured_at BETWEEN ? AND ? THEN 0 ELSE 1 END, "
            window_args = [time_window[0], time_window[1]]
        rows, basis = [], "fts"
        if terms:
            expression = " OR ".join('"' + x.replace('"', '""') + '"' for x in terms)
            rows = conn.execute(
                "SELECT a.*,p.name project_name,bm25(conversation_fts) rank FROM conversation_fts "
                "JOIN conversation_parts a ON a.archive_id=conversation_fts.archive_id "
                "JOIN projects p ON p.project_id=a.project_id WHERE conversation_fts MATCH ? AND " + gate +
                " ORDER BY " + window_order + "rank,a.captured_at DESC,a.archive_id LIMIT ?",
                [expression, *args, *window_args, limit]).fetchall()
        if not rows and time_window:
            # The question may carry only a time coordinate ("那会儿在弄啥?"),
            # or use wording the transcript never used. The window is still a
            # deterministic, already-authorized way to reach that moment, so
            # read the parts inside it directly -- same gate, same limit. They
            # carry no keyword match; the caller must accept that explicitly.
            rows = conn.execute(
                "SELECT a.*,p.name project_name,0.0 AS rank FROM conversation_parts a "
                "JOIN projects p ON p.project_id=a.project_id WHERE " + gate +
                " AND a.captured_at BETWEEN ? AND ? "
                "ORDER BY a.captured_at DESC,a.archive_id LIMIT ?",
                [*args, time_window[0], time_window[1], limit]).fetchall()
            basis = "time_window"
        def excerpt(row):
            text = row["text"]
            matches = [term for term in terms if term in text.lower()]
            positions = [text.lower().find(term) for term in matches]
            start = max(0, min(positions) - 100) if positions else 0
            return text[start:start + 600], matches
        return {"query_terms": terms, "items": [{"kind": "conversation_reference", "retrieval_basis": basis,
            "archive_id": r["archive_id"],
            "session_id": r["session_id"], "message_id": r["message_id"], "role": r["role"],
            "source_app": r["app"], "source_client_id": r["client_id"], "project_id": r["project_id"], "project_name": r["project_name"],
            "captured_at": r["captured_at"], "excerpt": excerpt(r)[0], "source_hash": r["text_hash"],
            "capture_completeness": r["capture_completeness"], "part_index": r["part_index"],
            "part_count": r["part_count"], "match_terms": excerpt(r)[1]} for r in rows]}


def recent_session(service, client, session_id, limit=6, max_chars=800):
    """Recent user/assistant text from this client's own session.

    This is only a query-building view for the current turn. It does not grant
    archive access, cross-session recall, or any task-association permission.
    """
    if (not isinstance(session_id, str) or not session_id or len(session_id) > 160
            or type(limit) is not int or not 1 <= limit <= 20
            or type(max_chars) is not int or not 100 <= max_chars <= 4000):
        raise Problem("VALIDATION_ERROR", "会话近况条件无效", 422)
    with service.db.read() as conn:
        client = current_client(conn, {"client_id": client["client_id"]})
        rows = conn.execute(
            "SELECT a.archive_id,a.message_id,a.role,a.app,a.captured_at,a.text,a.text_hash "
            "FROM conversation_parts a WHERE a.client_id=? AND a.session_id=? "
            "AND a.role IN ('user','assistant') AND a.scope_known=1 AND " + visible_sql() +
            " ORDER BY a.captured_at DESC,a.rowid DESC LIMIT ?",
            (client["client_id"], session_id, limit),
        ).fetchall()
        items = [{
            "archive_id": row["archive_id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "source_app": row["app"],
            "captured_at": row["captured_at"],
            "text": row["text"][:max_chars],
            "text_truncated": len(row["text"]) > max_chars,
            "source_hash": row["text_hash"],
        } for row in rows]
    items.reverse()
    return {"items": items, "limit": limit, "scope": "current_client_current_session",
            "purpose": "query_basis_only_not_injected_by_itself"}


def get(service, client, archive_id, offset=0):
    if not isinstance(archive_id, str) or type(offset) is not int or offset < 0:
        raise Problem("VALIDATION_ERROR", "来源页无效", 422)
    with service.db.read() as conn:
        gate, args = _gate(conn, client)
        row = conn.execute("SELECT a.* FROM conversation_parts a JOIN projects p ON p.project_id=a.project_id WHERE a.archive_id=? AND " + gate, [archive_id, *args]).fetchone()
        if not row:
            raise Problem("NOT_FOUND", "会话来源不可用", 404)
        text = row["text"]
        return {"archive_id": archive_id, "text": text[offset:offset + 1200], "offset": offset,
                "next_offset": offset + 1200 if offset + 1200 < len(text) else None,
                "source_hash": row["text_hash"], "capture_completeness": row["capture_completeness"]}


def delete_session(service, admin, client_id, session_id, revoke_memories=False):
    if admin.get("kind") != "admin":
        raise Problem("FORBIDDEN", "需要管理权限", 403)
    if type(revoke_memories) is not bool or not isinstance(session_id, str) or not 1 <= len(session_id) <= 160:
        raise Problem("VALIDATION_ERROR", "删除参数无效", 422)
    with service.db.tx() as conn:
        rows = conn.execute("SELECT DISTINCT event_id FROM conversation_parts WHERE client_id=? AND session_id=?", (client_id, session_id)).fetchall()
        events = [r[0] for r in rows]
        removed = 0
        if revoke_memories and events:
            memories = conn.execute("SELECT DISTINCT m.*,l.evidence_hash FROM memories m JOIN automation_memory_links l USING(memory_id) WHERE l.event_id IN (SELECT value FROM json_each(?))", (canonical(events),)).fetchall()
            for row in memories:
                conn.execute("INSERT OR IGNORE INTO automation_suppressions VALUES (?,?,?)", (row["project_id"], row["evidence_hash"], now()))
                if row["status"] != "Deleted":
                    service._change_row(conn, dict(row), {"status": "Deleted", "deleted_from_status": row["status"]}, admin, "automatic_revoke", "删除会话时撤销记忆", uid())
                    removed += 1
            conn.execute("UPDATE automation_jobs SET status='cancelled',error_code='USER_REVOKED',lease_token=NULL,lease_until=0,updated_at=? WHERE event_id IN (SELECT value FROM json_each(?)) AND status IN ('pending','running','failed')", (now(), canonical(events)))
        count = conn.execute("DELETE FROM conversation_parts WHERE client_id=? AND session_id=?", (client_id, session_id)).rowcount
        service._log(conn, admin, "conversation_delete", session_id)
    return {"deleted_parts": count, "revoked_memories": removed}
