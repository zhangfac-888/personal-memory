"""Explicit, transactional migrations for the automatic memory channel."""

AUTOMATION_V4 = """
CREATE TABLE automation_clients(
 client_id TEXT PRIMARY KEY, app TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 revoked INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, last_read_at TEXT, last_capture_at TEXT
);
CREATE TABLE workspace_bindings(
 binding_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
 project_id TEXT NOT NULL REFERENCES projects(project_id), roots_json TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE automation_events(
 event_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
 binding_id TEXT NOT NULL REFERENCES workspace_bindings(binding_id),
 session_id TEXT NOT NULL, turn_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(client_id,session_id,turn_id)
);
CREATE TABLE automation_jobs(
 job_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES automation_events(event_id),
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT, error_code TEXT, result_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX automation_jobs_due ON automation_jobs(status,next_attempt);
CREATE TABLE automation_memory_links(
 event_id TEXT NOT NULL REFERENCES automation_events(event_id),
 memory_id TEXT NOT NULL REFERENCES memories(memory_id), topic TEXT NOT NULL,
 source_role TEXT NOT NULL, evidence_hash TEXT NOT NULL,
 PRIMARY KEY(event_id,memory_id)
);
CREATE TABLE automation_suppressions(
 project_id TEXT NOT NULL REFERENCES projects(project_id), evidence_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(project_id,evidence_hash)
);
"""


def migrate_v4(conn, stamp):
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    if version < 4:
        # executescript commits an outstanding transaction. BEGIN belongs inside
        # the script so DDL and its version marker either both commit or neither.
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + AUTOMATION_V4)
            conn.execute("INSERT INTO schema_migrations VALUES (4,?)", (stamp,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


AUTOMATION_V5 = """
ALTER TABLE automation_clients ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'bound'
 CHECK(access_mode IN ('bound','library'));
ALTER TABLE automation_clients ADD COLUMN excluded_roots_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE workspace_aliases(
 alias_id TEXT PRIMARY KEY,
 client_id TEXT,
 root_key TEXT NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(project_id),
 resolution TEXT NOT NULL CHECK(resolution IN ('directory','git-root','personal')),
 created_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL,
 UNIQUE(root_key)
);
CREATE INDEX workspace_aliases_project ON workspace_aliases(project_id);
CREATE TABLE session_scopes(
 client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
 session_id TEXT NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(project_id),
 task_hint TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL,
 PRIMARY KEY(client_id,session_id)
);
"""


def migrate_v5(conn, stamp):
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    if version < 5:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + AUTOMATION_V5)
            conn.execute("INSERT INTO schema_migrations VALUES (5,?)", (stamp,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


AUTOMATION_V6 = """
ALTER TABLE automation_clients ADD COLUMN purpose TEXT NOT NULL DEFAULT 'automation'
 CHECK(purpose IN ('automation','mcp'));
"""


def migrate_v6(conn, stamp):
    """Separate automatic turn credentials from read-only MCP credentials."""
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    if version < 6:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + AUTOMATION_V6)
            conn.execute("INSERT INTO schema_migrations VALUES (6,?)", (stamp,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


AUTOMATION_V7 = """
CREATE TABLE workspace_roots(
 project_id TEXT NOT NULL REFERENCES projects(project_id),
 root TEXT NOT NULL, identity_basis TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(project_id,root)
);
CREATE INDEX workspace_roots_path ON workspace_roots(root);
CREATE TABLE source_scopes(
 source_id TEXT PRIMARY KEY REFERENCES sources(source_id),
 origin_project_id TEXT REFERENCES projects(project_id),
 root TEXT, known INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE automation_event_identity(
 event_id TEXT PRIMARY KEY REFERENCES automation_events(event_id),
 version INTEGER NOT NULL, fingerprint TEXT NOT NULL
);
ALTER TABLE automation_jobs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{"cursor":0,"items":[]}';
ALTER TABLE automation_jobs ADD COLUMN last_served REAL NOT NULL DEFAULT 0;
CREATE TABLE automation_job_history(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id TEXT NOT NULL REFERENCES automation_jobs(job_id),
 stage TEXT NOT NULL, code TEXT, created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE memory_knn USING vec0(memory_id TEXT PRIMARY KEY,vector FLOAT[1024] distance_metric=cosine);
CREATE TRIGGER memory_vectors_knn_delete AFTER DELETE ON memory_vectors BEGIN
 DELETE FROM memory_knn WHERE memory_id=old.memory_id;
END;
INSERT INTO memory_knn(memory_id,vector) SELECT memory_id,vector FROM memory_vectors;
"""


def migrate_v7(conn, stamp):
    """Only proven legacy roots are registered; unknown provenance stays unknown."""
    import json
    from pathlib import Path
    def normalized(value):
        return str(Path(value).expanduser().resolve()).replace("\\", "/").rstrip("/").casefold()
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    if version >= 7:
        return
    from .db import load_vector_extension
    load_vector_extension(conn)
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + AUTOMATION_V7)
        for row in conn.execute("SELECT project_id,roots_json FROM workspace_bindings").fetchall():
            for value in json.loads(row["roots_json"]):
                conn.execute("INSERT OR IGNORE INTO workspace_roots VALUES (?,?,?,?)",
                    (row["project_id"], normalized(value), "legacy-binding", stamp))
        for row in conn.execute("SELECT e.event_id,e.payload_json,b.project_id FROM automation_events e JOIN workspace_bindings b USING(binding_id)").fetchall():
            payload = json.loads(row["payload_json"])
            cwd = payload.get("workspace_root") or payload.get("cwd")
            root = normalized(cwd) if cwd else None
            if root:
                conn.execute("INSERT OR IGNORE INTO workspace_roots VALUES (?,?,?,?)",
                    (row["project_id"], root, "legacy-event", stamp))
            for source in conn.execute("SELECT DISTINCT m.source_id FROM automation_memory_links l JOIN memories m USING(memory_id) WHERE l.event_id=?", (row["event_id"],)).fetchall():
                conn.execute("INSERT OR IGNORE INTO source_scopes VALUES (?,?,?,?)",
                    (source["source_id"], row["project_id"], root, int(bool(root) or row["project_id"] == "personal-sessions")))
        conn.execute("INSERT OR IGNORE INTO source_scopes(source_id,known) SELECT source_id,0 FROM sources WHERE source_type='automatic'")
        conn.execute("INSERT INTO schema_migrations VALUES (7,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v8(conn, stamp):
    """Keep captured text independently of short-lived extraction events."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 8:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        ALTER TABLE automation_clients ADD COLUMN archive_read INTEGER NOT NULL DEFAULT 0 CHECK(archive_read IN (0,1));
        ALTER TABLE automation_jobs ADD COLUMN model_attempts INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE automation_jobs ADD COLUMN provider_calls INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE automation_jobs ADD COLUMN use_fallback INTEGER NOT NULL DEFAULT 0 CHECK(use_fallback IN (0,1));
        ALTER TABLE automation_jobs ADD COLUMN bounded_provider INTEGER NOT NULL DEFAULT 0 CHECK(bounded_provider IN (0,1));
        CREATE TABLE conversation_parts(
          archive_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES automation_events(event_id),
          client_id TEXT NOT NULL REFERENCES automation_clients(client_id), app TEXT NOT NULL,
          session_id TEXT NOT NULL, original_turn_id TEXT NOT NULL, message_id TEXT NOT NULL,
          role TEXT NOT NULL, part_index INTEGER NOT NULL, part_count INTEGER NOT NULL,
          turn_part_index INTEGER NOT NULL, turn_part_count INTEGER NOT NULL,
          project_id TEXT NOT NULL REFERENCES projects(project_id), origin_root TEXT,
          scope_known INTEGER NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
          full_text_hash TEXT, captured_at TEXT NOT NULL, completion_state TEXT NOT NULL,
          capture_completeness TEXT NOT NULL,
          UNIQUE(client_id,session_id,original_turn_id,message_id,part_index)
        );
        CREATE INDEX conversation_session ON conversation_parts(client_id,session_id,original_turn_id);
        CREATE INDEX conversation_scope ON conversation_parts(project_id,captured_at);
        CREATE VIRTUAL TABLE conversation_fts USING fts5(archive_id UNINDEXED,tokens);
        CREATE TRIGGER conversation_fts_delete AFTER DELETE ON conversation_parts BEGIN
          DELETE FROM conversation_fts WHERE archive_id=old.archive_id;
        END;
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (8,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v9(conn, stamp):
    """Add execution metadata without changing existing memory classifications."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)>=9:return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        ALTER TABLE memories ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'memory' CHECK(record_kind IN ('memory','plan'));
        ALTER TABLE memories ADD COLUMN plan_status TEXT CHECK(plan_status IN ('planned','in_progress','blocked','postponed','completed','cancelled'));
        ALTER TABLE memories ADD COLUMN plan_key TEXT CHECK(
          (record_kind='memory' AND plan_status IS NULL AND plan_key IS NULL) OR
          (record_kind='plan' AND type IN ('Fact','Decision') AND plan_status IS NOT NULL AND plan_key IS NOT NULL AND length(trim(plan_key))>0));
        CREATE INDEX memories_plan_scope ON memories(project_id,record_kind,plan_status,status,updated_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (9,?)",(stamp,));conn.commit()
    except BaseException:
        conn.rollback();raise


def migrate_v10(conn, stamp):
    """Task checkpoints reference archives; grants do not open raw history."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 10:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE task_checkpoint_grants(
          client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
          project_id TEXT NOT NULL REFERENCES projects(project_id),
          allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), updated_at TEXT NOT NULL,
          PRIMARY KEY(client_id,project_id)
        );
        CREATE TABLE task_checkpoints(
          task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
          origin_client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
          origin_session_id TEXT NOT NULL, origin_root TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE task_checkpoint_sessions(
          client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
          session_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(project_id),
          task_id TEXT NOT NULL REFERENCES task_checkpoints(task_id), last_seen_at TEXT NOT NULL,
          PRIMARY KEY(client_id,session_id,project_id)
        );
        CREATE INDEX task_checkpoint_members ON task_checkpoint_sessions(task_id);
        CREATE INDEX task_checkpoint_scope ON task_checkpoints(project_id,created_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (10,?)",(stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v11(conn, stamp):
    """Reversible checkpoint overlays; original conversations are never rewritten."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)>=11:return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE task_checkpoint_annotations(
          annotation_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES task_checkpoints(task_id),
          subject_archive_id TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('phase','execution')),
          data_json TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, revoked_at TEXT
        );
        CREATE INDEX task_annotation_scope ON task_checkpoint_annotations(task_id,revoked_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (11,?)",(stamp,));conn.commit()
    except BaseException:
        conn.rollback();raise


def migrate_v12(conn,stamp):
    """Explicit automatic policy decisions; no historical memory promotion."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)>=12:return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE automatic_decisions(
          decision_id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES automation_events(event_id),
          memory_id TEXT REFERENCES memories(memory_id),project_id TEXT NOT NULL REFERENCES projects(project_id),
          task_id TEXT,source_role TEXT NOT NULL,evidence_hash TEXT NOT NULL,
          outcome TEXT NOT NULL CHECK(outcome IN ('active','review','skipped','suppressed')),
          reasons_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE INDEX automatic_decisions_recent ON automatic_decisions(created_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (12,?)",(stamp,));conn.commit()
    except BaseException:conn.rollback();raise


def migrate_v13(conn, stamp):
    """Hash-bound source corrections without changing captured text or roles."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 13:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE conversation_source_corrections(
          correction_id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL REFERENCES automation_events(event_id),
          event_content_hash TEXT NOT NULL,
          reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT
        );
        CREATE INDEX conversation_source_correction_event ON conversation_source_corrections(event_id,revoked_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (13,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v14(conn, stamp):
    """Explicit reversible directory routing; never merge or backfill history."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 14:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE workspace_directory_mappings(
          mapping_id TEXT PRIMARY KEY, directory TEXT NOT NULL UNIQUE,
          workspace_root TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(project_id),
          active INTEGER NOT NULL CHECK(active IN (0,1)),
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX workspace_mapping_project ON workspace_directory_mappings(project_id);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (14,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v15(conn, stamp):
    """Versioned task continuation contracts and host-observed tool actions.

    Additive only: no historical archive, checkpoint or memory row is changed.
    """
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 15:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE task_contracts(
          task_id TEXT NOT NULL REFERENCES task_checkpoints(task_id),
          version INTEGER NOT NULL CHECK(version>=1),
          data_json TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
          created_at TEXT NOT NULL, restored_from INTEGER,
          PRIMARY KEY(task_id,version)
        );
        CREATE TABLE task_contract_actions(
          action_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES task_checkpoints(task_id),
          contract_version INTEGER,
          client_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT,
          tool_use_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
          phase TEXT NOT NULL CHECK(phase IN ('pre','post')),
          tool_name TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('allow','deny','not_covered','observed')),
          rule TEXT NOT NULL, step_id TEXT, check_id TEXT,
          input_sha256 TEXT NOT NULL, output_sha256 TEXT, exit_code INTEGER,
          exit_code_basis TEXT, artifact_json TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX task_contract_actions_task ON task_contract_actions(task_id,created_at);
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (15,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_v16(conn, stamp):
    """Pre-capture reservations only; no historical progress or grants are filled in."""
    if (conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0) >= 16:
        return
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE task_preparations(
          task_id TEXT PRIMARY KEY REFERENCES task_checkpoints(task_id),
          project_id TEXT NOT NULL REFERENCES projects(project_id),
          producer_client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
          producer_session_id TEXT NOT NULL,
          consumer_client_id TEXT NOT NULL REFERENCES automation_clients(client_id),
          workspace_root TEXT NOT NULL, root_identity_json TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('reserved','captured','cancelled')),
          revision INTEGER NOT NULL CHECK(revision>=1),
          idempotency_key TEXT NOT NULL UNIQUE, definition_hash TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(producer_client_id,producer_session_id)
        );
        """)
        conn.execute("INSERT INTO schema_migrations VALUES (16,?)", (stamp,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
