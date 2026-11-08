from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 project_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS projects_name_active ON projects(lower(name)) WHERE status='active';
CREATE TABLE IF NOT EXISTS sources(
 source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 source_type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 origin_app TEXT, external_ref TEXT, source_created_at TEXT,
 created_at TEXT NOT NULL, content_hash TEXT NOT NULL,
 visibility TEXT NOT NULL CHECK(visibility IN ('local_only','agent_allowed')),
 version INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories(
 memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 content TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('Fact','Decision','Preference','Rule','Experience','OpenQuestion')),
 status TEXT NOT NULL CHECK(status IN ('Candidate','Active','Superseded','Archived','Deleted')),
 confidence TEXT NOT NULL CHECK(confidence IN ('Confirmed','Reported','Inferred')),
 source_id TEXT NOT NULL REFERENCES sources(source_id), source_start INTEGER NOT NULL,
 source_end INTEGER NOT NULL, source_quote TEXT NOT NULL, decision_key TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 superseded_by TEXT REFERENCES memories(memory_id), deleted_from_status TEXT,
 created_by TEXT NOT NULL, last_modified_by TEXT NOT NULL,
 CHECK(status!='Active' OR confidence!='Inferred'),
 CHECK(type!='Decision' OR (decision_key IS NOT NULL AND length(trim(decision_key))>0))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_decision
 ON memories(project_id,decision_key) WHERE type='Decision' AND status='Active';
CREATE INDEX IF NOT EXISTS memories_scope ON memories(project_id,status,type);
CREATE TABLE IF NOT EXISTS memory_revisions(
 revision_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(memory_id),
 version INTEGER NOT NULL, operation_id TEXT NOT NULL, action TEXT NOT NULL,
 before_snapshot TEXT, after_snapshot TEXT NOT NULL, reason TEXT NOT NULL,
 actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(memory_id,version)
);
CREATE TABLE IF NOT EXISTS change_requests(
 request_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES memories(memory_id),
 action TEXT NOT NULL CHECK(action IN ('edit','supersede')),
 base_version INTEGER NOT NULL, proposed_payload TEXT NOT NULL,
 reason TEXT NOT NULL, actor_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','conflict')),
 created_at TEXT NOT NULL, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_records(
 actor_id TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(actor_id,operation,key)
);
CREATE TABLE IF NOT EXISTS client_credentials(
 client_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 project_ids TEXT NOT NULL, can_write INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_log(
 operation_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
 object_id TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED,tokens);
CREATE TABLE IF NOT EXISTS search_index_meta(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), tokenizer_version TEXT NOT NULL,
 rebuilt_at TEXT NOT NULL, indexed_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS extraction_jobs(
 job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
 project_id TEXT NOT NULL REFERENCES projects(project_id),
 selection_start INTEGER NOT NULL, selection_end INTEGER NOT NULL,
 text_sha256 TEXT NOT NULL, provider TEXT NOT NULL, model_id TEXT NOT NULL,
 model_digest TEXT NOT NULL, prompt_version TEXT NOT NULL, response_schema_version INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted')),
 error_code TEXT, candidate_ids TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS extraction_jobs_project ON extraction_jobs(project_id,created_at);
CREATE TABLE IF NOT EXISTS memory_vectors(
 memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
 memory_version INTEGER NOT NULL, model_id TEXT NOT NULL, model_digest TEXT NOT NULL,
 dimensions INTEGER NOT NULL, vector BLOB NOT NULL, indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_jobs(
 job_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted')),
 model_id TEXT NOT NULL, model_digest TEXT NOT NULL, total_count INTEGER NOT NULL DEFAULT 0,
 indexed_count INTEGER NOT NULL DEFAULT 0, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def load_vector_extension(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            if existing:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}，未作任何迁移")
            conn.executescript(SCHEMA)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (1, now))
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (3, now))
            from .migrations import migrate_v4, migrate_v5, migrate_v6, migrate_v7, migrate_v8, migrate_v9, migrate_v10, migrate_v11, migrate_v12, migrate_v13, migrate_v14, migrate_v15, migrate_v16
            migrate_v4(conn, now)
            migrate_v5(conn, now)
            migrate_v6(conn, now)
            migrate_v7(conn, now)
            migrate_v8(conn, now)
            migrate_v9(conn, now)
            migrate_v10(conn, now)
            migrate_v11(conn, now)
            migrate_v12(conn, now)
            migrate_v13(conn, now)
            migrate_v14(conn, now)
            migrate_v15(conn, now)
            migrate_v16(conn, now)
            conn.execute("INSERT OR IGNORE INTO projects(project_id,name,description,created_at,updated_at) VALUES ('global','Global','',?,?)", (now,now))
            conn.execute("INSERT OR IGNORE INTO projects(project_id,name,description,created_at,updated_at) VALUES ('personal-sessions','个人会话','没有可靠项目目录时仍可积累的个人记忆',?,?)", (now,now))
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        load_vector_extension(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def tx(self,timeout_ms=None):
        conn = self.connect()
        try:
            if timeout_ms is not None:
                conn.execute("PRAGMA busy_timeout="+str(max(1,int(timeout_ms))))
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def readonly(self):
        """SELECT-only connection, without journal initialization or migrations."""
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
