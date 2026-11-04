from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import jieba

from .db import Database, SCHEMA_VERSION
from . import plans
from .evidence_scope import detail_scope, without_audit_accounts, answer_evidence

TYPES = {"Fact", "Decision", "Preference", "Rule", "Experience", "OpenQuestion"}
STATUSES = {"Candidate", "Active", "Superseded", "Archived", "Deleted"}
CONFIDENCES = {"Confirmed", "Reported", "Inferred"}
APP_VERSION = "0.1.0"
TOKENIZER_VERSION = "jieba-" + jieba.__version__ + "-dict-" + hashlib.sha256(Path(jieba.__file__).with_name("dict.txt").read_bytes()).hexdigest() + "-code-v1"
QUERY_STOPWORDS = frozenset({"的", "了", "和", "与", "及", "是", "在", "the", "a", "an", "of", "to", "and", "is"})
SNAPSHOT_FIELDS = ("memory_id", "project_id", "content", "type", "status", "confidence", "source_id", "source_start", "source_end", "source_quote", "decision_key", "created_at", "updated_at", "version", "superseded_by", "deleted_from_status", "created_by", "last_modified_by", "record_kind", "plan_status", "plan_key")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tokenize(value: str) -> list[str]:
    result = []
    for segment in jieba.lcut(value.lower()):
        result.extend(re.findall(r"[\w]+", segment, re.UNICODE))
    return [part for part in result if part and part != "_"]


class Problem(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class MemoryService:
    def __init__(self, db: Database):
        self.db = db
        self._ensure_search_index()

    def _ensure_search_index(self):
        with self.db.tx() as conn:
            meta=conn.execute("SELECT * FROM search_index_meta WHERE singleton=1").fetchone()
            active_count=conn.execute("SELECT COUNT(*) FROM memories WHERE status='Active'").fetchone()[0]
            indexed_count=conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
            if meta and meta["tokenizer_version"]==TOKENIZER_VERSION and meta["indexed_count"]==active_count and indexed_count==active_count:
                return
            conn.execute("DELETE FROM memory_fts")
            rows=conn.execute("SELECT m.memory_id,m.content,s.title FROM memories m JOIN sources s ON s.source_id=m.source_id WHERE m.status='Active'")
            count=0
            for row in rows:
                conn.execute("INSERT INTO memory_fts(memory_id,tokens) VALUES (?,?)",(row["memory_id"]," ".join(tokenize(row["content"]+" "+row["title"]))))
                count+=1
            if count!=active_count or conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]!=active_count:
                raise RuntimeError("搜索索引重建校验失败")
            conn.execute("INSERT INTO search_index_meta(singleton,tokenizer_version,rebuilt_at,indexed_count) VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET tokenizer_version=excluded.tokenizer_version,rebuilt_at=excluded.rebuilt_at,indexed_count=excluded.indexed_count",(TOKENIZER_VERSION,now(),count))

    def _scope(self, actor: dict, project_id: str, write: bool = False):
        if actor["kind"] == "admin":
            return
        if actor.get("library"):
            if write:
                raise Problem("FORBIDDEN", "库级检索凭据不能直接写入记忆", 403)
            if not actor.get("registered_library") and project_id in set(actor.get("excluded_projects") or ()):
                raise Problem("NOT_FOUND", "记忆或项目不可用", 404)
            if project_id in {"global", "personal-sessions"}:
                return
            # A library client may read automatic projects, but only its
            # explicit current project is considered current context.
            with self.db.read() as conn:
                if not conn.execute("SELECT 1 FROM projects WHERE project_id=? AND status='active'", (project_id,)).fetchone():
                    raise Problem("NOT_FOUND", "记忆或项目不可用", 404)
            return
        if project_id not in actor["projects"]:
            raise Problem("NOT_FOUND", "记忆或项目不可用", 404)
        if write and not actor["can_write"]:
            raise Problem("FORBIDDEN", "此客户端只有读取权限", 403)

    def _project(self, conn, project_id: str, active: bool = False):
        row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            raise Problem("NOT_FOUND", "项目不存在", 404)
        if active and row["status"] != "active":
            raise Problem("INVALID_TRANSITION", "项目已归档")
        return dict(row)

    def _source(self, conn, source_id: str, project_id: str, actor: dict):
        row = conn.execute("SELECT * FROM sources WHERE source_id=? AND project_id=?", (source_id, project_id)).fetchone()
        if not row or (actor["kind"] != "admin" and row["visibility"] != "agent_allowed"):
            raise Problem("NOT_FOUND", "来源不可用", 404)
        gate, args = self._read_filter(conn, actor, source="s", project="s.project_id")
        if not conn.execute("SELECT 1 FROM sources s WHERE s.source_id=? AND " + gate, [source_id, *args]).fetchone():
            raise Problem("NOT_FOUND", "来源不可用", 404)
        return dict(row)

    def _read_filter(self, conn, actor, source="s", project="m.project_id"):
        """One current policy gate for search, vectors, sources and revisions."""
        if actor["kind"] == "admin":
            return "1", []
        clauses = [source + ".visibility='agent_allowed'"]
        args = []
        if actor.get("library"):
            from .workspaces import current_client, effective_exclusions
            roots = []
            if actor.get("registered_library"):
                client = current_client(conn, {"client_id": actor["id"]})
                roots = effective_exclusions(conn, client)
            else:
                excluded = actor.get("excluded_projects") or []
                if excluded:
                    clauses.append(project + " NOT IN (SELECT value FROM json_each(?))")
                    args.append(canonical(excluded))
            if roots:
                # Filtering precedes LIMIT and ranking. Provenance survives raw
                # event retention and Global promotion.
                match = "(wr.root=x.value OR substr(wr.root,1,length(x.value)+1)=x.value||'/')"
                blocked = "SELECT 1 FROM workspace_roots wr,json_each(?) x WHERE wr.project_id=" + project + " AND " + match
                clauses.append("NOT EXISTS (" + blocked + ")")
                args.append(canonical(roots))
                clauses.append("NOT EXISTS (SELECT 1 FROM source_scopes ss WHERE ss.source_id=" + source + ".source_id AND (ss.known=0 OR EXISTS (SELECT 1 FROM workspace_roots wr,json_each(?) x WHERE wr.project_id=ss.origin_project_id AND " + match + ")))")
                args.append(canonical(roots))
                clauses.append("(" + source + ".source_type!='automatic' OR EXISTS (SELECT 1 FROM source_scopes ss WHERE ss.source_id=" + source + ".source_id AND ss.known=1))")
        return " AND ".join(clauses), args

    def _memory(self, conn, memory_id: str, actor: dict, allow_deleted: bool = False):
        row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if not row:
            raise Problem("NOT_FOUND", "记忆不可用", 404)
        row = dict(row)
        self._scope(actor, row["project_id"])
        if row["status"] == "Deleted" and (actor["kind"] != "admin" or not allow_deleted):
            raise Problem("NOT_FOUND", "记忆不可用", 404)
        self._source(conn, row["source_id"], row["project_id"], actor)
        return row

    def _idempotent(self, conn, actor: dict, operation: str, key: str, request: dict):
        if not key or len(key) > 120:
            raise Problem("VALIDATION_ERROR", "需要长度不超过 120 的幂等键", 422)
        hash_value = digest(canonical(request))
        row = conn.execute("SELECT * FROM idempotency_records WHERE actor_id=? AND operation=? AND key=?", (actor["id"], operation, key)).fetchone()
        if row:
            if row["request_hash"] != hash_value:
                raise Problem("IDEMPOTENCY_CONFLICT", "该请求键已用于不同内容")
            result = json.loads(row["result_json"])
            result["replayed"] = True
            return result, hash_value
        return None, hash_value

    def _save_idempotent(self, conn, actor, operation, key, hash_value, result):
        conn.execute("INSERT INTO idempotency_records VALUES (?,?,?,?,?,?)", (actor["id"],operation,key,hash_value,canonical(result),now()))

    def _log(self, conn, actor, action, object_id, operation_id=None):
        conn.execute("INSERT INTO operation_log VALUES (?,?,?,?,?,?)", (operation_id or uid(),actor["id"],action,object_id,"success",now()))

    def _snapshot(self, row):
        return {key: row[key] for key in SNAPSHOT_FIELDS}

    def _revision(self, conn, before, after, action, reason, actor, operation_id):
        conn.execute("INSERT INTO memory_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uid(),after["memory_id"],after["version"],operation_id,action,canonical(self._snapshot(before)) if before else None,canonical(self._snapshot(after)),reason,actor["kind"],actor["id"],now()))

    def _index(self, conn, row):
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
        conn.execute("DELETE FROM memory_vectors WHERE memory_id=?", (row["memory_id"],))
        if row["status"] == "Active":
            source = conn.execute("SELECT title FROM sources WHERE source_id=?", (row["source_id"],)).fetchone()
            terms = tokenize(row["content"] + " " + (source["title"] if source else ""))
            conn.execute("INSERT INTO memory_fts(memory_id,tokens) VALUES (?,?)", (row["memory_id"]," ".join(terms)))
        conn.execute("UPDATE search_index_meta SET indexed_count=(SELECT COUNT(*) FROM memory_fts) WHERE singleton=1")

    def _span(self, source, span):
        if span is None:
            span = {"start":0,"end":len(source["content"]),"quote":source["content"]}
        try:
            start, end, quote = int(span["start"]), int(span["end"]), span["quote"]
        except (KeyError, TypeError, ValueError):
            raise Problem("VALIDATION_ERROR", "来源位置格式错误", 422)
        if start < 0 or end <= start or end > len(source["content"]) or source["content"][start:end] != quote:
            raise Problem("VALIDATION_ERROR", "来源片段与原文不一致", 422)
        return start, end, quote

    def _validate_memory(self, content, type_, confidence, decision_key):
        if not isinstance(content, str) or not content.strip() or len(content) > 2000 or type_ not in TYPES or confidence not in CONFIDENCES:
            raise Problem("VALIDATION_ERROR", "记忆内容、类型或证据等级无效", 422)
        if type_ == "Decision" and (not isinstance(decision_key, str) or not decision_key.strip() or len(decision_key) > 120):
            raise Problem("VALIDATION_ERROR", "决策需要主题", 422)
        if type_ != "Decision" and decision_key:
            raise Problem("VALIDATION_ERROR", "此记忆类型不使用决策主题", 422)

    def list_projects(self, actor):
        with self.db.read() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY CASE WHEN project_id='global' THEN 0 ELSE 1 END,name")]
            if actor.get("library"):
                from .workspaces import current_client, excluded_projects
                excluded = excluded_projects(conn,current_client(conn,{"client_id":actor["id"]})) if actor.get("registered_library") else set(actor.get("excluded_projects",[]))
                return [r for r in rows if r["project_id"] not in excluded]
            return [r for r in rows if actor["kind"] == "admin" or r["project_id"] in actor["projects"]]

    def create_project(self, actor, name, description, key):
        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "只有管理员可创建项目", 403)
        name = name.strip()
        if not name or len(name)>100 or len(description)>1000:
            raise Problem("VALIDATION_ERROR", "项目名称或说明无效", 422)
        req = {"name":name,"description":description}
        with self.db.tx() as conn:
            replay, hashed = self._idempotent(conn,actor,"create_project",key,req)
            if replay: return replay
            ident, stamp = uid(), now()
            try:
                conn.execute("INSERT INTO projects(project_id,name,description,created_at,updated_at) VALUES (?,?,?,?,?)", (ident,name,description,stamp,stamp))
            except sqlite3.IntegrityError:
                raise Problem("VALIDATION_ERROR", "项目名称已存在", 422)
            result = {"project_id":ident,"name":name,"status":"active","version":1}
            self._save_idempotent(conn,actor,"create_project",key,hashed,result)
            self._log(conn,actor,"create_project",ident)
            return result

    def update_project(self,actor,project_id,changes,key):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可管理项目",403)
        if project_id=="global": raise Problem("INVALID_TRANSITION","Global 项目不能修改")
        expected=changes["expected_version"]
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"update_project",key,{"id":project_id,"changes":changes})
            if replay: return replay
            row=self._project(conn,project_id)
            if row["version"]!=expected: raise Problem("VERSION_CONFLICT","项目版本已变化")
            name=changes.get("name",row["name"])
            desc=changes.get("description",row["description"])
            status=changes.get("status",row["status"])
            if not isinstance(name,str) or not name.strip() or len(name)>100 or not isinstance(desc,str) or len(desc)>1000 or status not in ("active","archived") or not changes["reason"].strip():
                raise Problem("VALIDATION_ERROR","项目修改无效",422)
            try:
                conn.execute("UPDATE projects SET name=?,description=?,status=?,version=version+1,updated_at=? WHERE project_id=? AND version=?",(name.strip(),desc,status,now(),project_id,expected))
            except sqlite3.IntegrityError: raise Problem("VALIDATION_ERROR","项目名称已存在",422)
            result={"project_id":project_id,"name":name.strip(),"status":status,"version":expected+1}
            self._save_idempotent(conn,actor,"update_project",key,hashed,result)
            self._log(conn,actor,"update_project",project_id)
            return result

    def _create_source(self, conn, actor, project_id, source_content, source_title, visibility, source_type="manual"):
        if not isinstance(source_content,str) or not source_content.strip() or len(source_content)>200000 or not source_title or len(source_title)>200:
            raise Problem("VALIDATION_ERROR", "来源内容或标题无效", 422)
        if visibility not in ("local_only","agent_allowed") or (actor["kind"] != "admin" and visibility != "agent_allowed"):
            raise Problem("FORBIDDEN", "来源可见性不可用", 403)
        ident = uid()
        conn.execute("INSERT INTO sources(source_id,project_id,source_type,title,content,created_at,content_hash,visibility,created_by) VALUES (?,?,?,?,?,?,?,?,?)", (ident,project_id,source_type,source_title,source_content,now(),digest(source_content),visibility,actor["id"]))
        return self._source(conn,ident,project_id,actor)

    def create_source(self,actor,project_id,content,title,visibility,key):
        self._scope(actor,project_id,True)
        request={"project_id":project_id,"content":content,"title":title,"visibility":visibility}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"create_source",key,request)
            if replay:
                self._source(conn,replay["source_id"],project_id,actor)
                return replay
            self._project(conn,project_id,True)
            source=self._create_source(conn,actor,project_id,content,title,visibility)
            result={"source_id":source["source_id"],"project_id":project_id,"visibility":visibility,"version":1}
            self._save_idempotent(conn,actor,"create_source",key,hashed,result)
            self._log(conn,actor,"create_source",source["source_id"])
            return result

    def list_sources(self, actor, project_id, limit=100):
        self._scope(actor, project_id)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise Problem("VALIDATION_ERROR", "来源数量无效", 422)
        with self.db.read() as conn:
            self._project(conn, project_id)
            gate,args=self._read_filter(conn,actor,source="s",project="s.project_id")
            sql = "SELECT source_id,project_id,source_type,title,visibility,created_at,length(content) character_count FROM sources s WHERE project_id=? AND "+gate
            sql += " ORDER BY created_at DESC,source_id DESC LIMIT ?"
            return [dict(row) for row in conn.execute(sql, [project_id,*args,limit])]

    def import_file_source(self, actor, project_id, upload, expected_text_sha256, key):
        from .file_import import decode_upload

        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "仅本地管理端可导入文件", 403)
        decoded = decode_upload(upload["filename"], upload["base64"], upload["encoding"])
        if expected_text_sha256 != decoded["text_sha256"]:
            raise Problem("VERSION_CONFLICT", "文件与预览内容不一致，请重新预览", 409)
        request = {"project_id": project_id, "filename": decoded["filename"], "encoding": decoded["encoding"], "byte_sha256": decoded["byte_sha256"], "text_sha256": decoded["text_sha256"]}
        with self.db.tx() as conn:
            replay, hashed = self._idempotent(conn, actor, "import_file", key, request)
            if replay:
                self._source(conn, replay["source_id"], project_id, actor)
                return replay
            self._project(conn, project_id, True)
            source = self._create_source(conn, actor, project_id, decoded["content"], decoded["filename"], "local_only", "file")
            result = {"source_id": source["source_id"], "project_id": project_id, "visibility": "local_only", "source_type": "file", "text_sha256": decoded["text_sha256"], "character_count": decoded["character_count"]}
            self._save_idempotent(conn, actor, "import_file", key, hashed, result)
            self._log(conn, actor, "import_file", source["source_id"])
            return result

    def create_memory(self, actor, *, project_id, content, type, confidence, source_content=None, source_title=None, source_id=None, source_span=None, decision_key=None, visibility="local_only", activate=False, idempotency_key):
        request = dict(project_id=project_id,content=content,type=type,confidence=confidence,source_content=source_content,source_title=source_title,source_id=source_id,source_span=source_span,decision_key=decision_key,visibility=visibility,activate=activate)
        self._scope(actor,project_id,True)
        self._validate_memory(content,type,confidence,decision_key)
        if (source_content is None) == (source_id is None):
            raise Problem("VALIDATION_ERROR", "原文和来源 ID 必须二选一", 422)
        if activate and actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "Agent 只能提交候选", 403)
        if activate and confidence == "Inferred":
            raise Problem("VALIDATION_ERROR", "推断不能直接生效", 422)
        with self.db.tx() as conn:
            replay, hashed = self._idempotent(conn,actor,"create_memory",idempotency_key,request)
            if replay:
                current=self._memory(conn,replay["memory_id"],actor)
                replay["current_status"]=current["status"]
                replay["current_version"]=current["version"]
                return replay
            self._project(conn,project_id,True)
            source = self._create_source(conn,actor,project_id,source_content,source_title or "手动输入",visibility,"mcp" if actor["kind"]=="client" else "manual") if source_content is not None else self._source(conn,source_id,project_id,actor)
            start,end,quote = self._span(source,source_span)
            ident, stamp = uid(), now()
            plan = plans.columns(content,quote) if type in {"Fact","Decision"} else plans.columns("", "")
            status = "Active" if activate else "Candidate"
            try:
                conn.execute("INSERT INTO memories(memory_id,project_id,content,type,status,confidence,source_id,source_start,source_end,source_quote,decision_key,created_at,updated_at,created_by,last_modified_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident,project_id,content,type,status,confidence,source["source_id"],start,end,quote,decision_key.strip() if decision_key else None,stamp,stamp,actor["id"],actor["id"]))
                conn.execute("UPDATE memories SET record_kind=?,plan_status=?,plan_key=? WHERE memory_id=?",(*[plan[k] for k in plans.COLUMNS],ident))
            except sqlite3.IntegrityError:
                raise Problem("DECISION_CONFLICT", "同主题已有有效决策，请使用替代")
            row = dict(conn.execute("SELECT * FROM memories WHERE memory_id=?",(ident,)).fetchone())
            self._revision(conn,None,row,"create","创建",actor,uid())
            self._index(conn,row)
            result = {"memory_id":ident,"status":status,"version":1,"source_id":source["source_id"]}
            self._save_idempotent(conn,actor,"create_memory",idempotency_key,hashed,result)
            self._log(conn,actor,"create_memory",ident)
            return result

    def _change_row(self, conn, before, changes, actor, action, reason, operation_id):
        after = dict(before)
        after.update(changes)
        evidence_changed = bool(set(changes) & {"content","type","source_id","source_start","source_end","source_quote"})
        # Validity/history changes must not reinterpret preserved evidence with
        # today's grammar. Actual evidence edits still require reclassification.
        plan = (plans.columns(after["content"],after["source_quote"]) if after["type"] in {"Fact","Decision"} else plans.columns("", "")) if evidence_changed else {key:before[key] for key in plans.COLUMNS}
        if before.get("record_kind")=="plan" and plan["record_kind"]!="plan":
            raise Problem("VALIDATION_ERROR","计划修正需保留可核对的完整任务状态原文",422)
        after.update(plan)
        after["version"] = before["version"] + 1
        after["updated_at"] = now()
        after["last_modified_by"] = actor["id"]
        conn.execute("UPDATE memories SET content=?,status=?,confidence=?,source_id=?,source_start=?,source_end=?,source_quote=?,decision_key=?,updated_at=?,version=?,superseded_by=?,deleted_from_status=?,last_modified_by=?,record_kind=?,plan_status=?,plan_key=? WHERE memory_id=? AND version=?", (after["content"],after["status"],after["confidence"],after["source_id"],after["source_start"],after["source_end"],after["source_quote"],after["decision_key"],after["updated_at"],after["version"],after["superseded_by"],after["deleted_from_status"],after["last_modified_by"],after["record_kind"],after["plan_status"],after["plan_key"],after["memory_id"],before["version"]))
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise Problem("VERSION_CONFLICT", "记忆已被他人修改，请重新查看")
        self._revision(conn,before,after,action,reason,actor,operation_id)
        self._index(conn,after)
        return after

    def _check_version(self,row,expected):
        if row["version"] != expected:
            raise Problem("VERSION_CONFLICT", "记忆版本已变化，请重新查看")

    def approve_candidate(self, actor, memory_id, expected_version, key):
        if actor["kind"] != "admin": raise Problem("FORBIDDEN","只有管理员可审批",403)
        request={"id":memory_id,"version":expected_version}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"approve_candidate",key,request)
            if replay: return replay
            row=self._memory(conn,memory_id,actor)
            self._check_version(row,expected_version)
            if row["status"]!="Candidate" or row["confidence"]=="Inferred":
                raise Problem("INVALID_TRANSITION","这条候选尚不能激活")
            self._project(conn,row["project_id"],True)
            try: after=self._change_row(conn,row,{"status":"Active"},actor,"approve","用户批准",uid())
            except sqlite3.IntegrityError: raise Problem("DECISION_CONFLICT","同主题已有有效决策，请使用替代")
            result={"memory_id":memory_id,"status":after["status"],"version":after["version"]}
            self._save_idempotent(conn,actor,"approve_candidate",key,hashed,result)
            self._log(conn,actor,"approve_candidate",memory_id)
            return result

    def edit_memory(self,actor,memory_id,expected_version,changes,reason,key):
        request={"id":memory_id,"version":expected_version,"changes":changes,"reason":reason}
        allowed={"content","confidence","source_id","source_span","decision_key"}
        if not changes or set(changes)-allowed or not reason.strip():
            raise Problem("VALIDATION_ERROR","修改字段或原因无效",422)
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"edit_memory",key,request)
            if replay:
                self._memory(conn,memory_id,actor)
                return replay
            row=self._memory(conn,memory_id,actor)
            self._scope(actor,row["project_id"],True)
            self._check_version(row,expected_version)
            if row["status"] not in ("Candidate","Active") or (actor["kind"]!="admin" and (row["status"]!="Candidate" or row["created_by"]!=actor["id"])):
                raise Problem("FORBIDDEN","此记忆不能直接编辑",403)
            content=changes.get("content",row["content"]); confidence=changes.get("confidence",row["confidence"])
            decision_key=changes.get("decision_key",row["decision_key"])
            if isinstance(decision_key,str): decision_key=decision_key.strip()
            self._validate_memory(content,row["type"],confidence,decision_key)
            if row["status"]=="Active" and confidence=="Inferred":
                raise Problem("VALIDATION_ERROR","推断不能为有效记忆",422)
            source=self._source(conn,changes.get("source_id",row["source_id"]),row["project_id"],actor)
            if "source_id" in changes and "source_span" not in changes:
                raise Problem("VALIDATION_ERROR","更换来源时必须指定原文片段",422)
            start,end,quote=self._span(source,changes.get("source_span",{"start":row["source_start"],"end":row["source_end"],"quote":row["source_quote"]}))
            update={"content":content,"confidence":confidence,"decision_key":decision_key,"source_id":source["source_id"],"source_start":start,"source_end":end,"source_quote":quote}
            if all(row[k]==v for k,v in update.items()):
                result={"memory_id":memory_id,"status":row["status"],"version":row["version"],"unchanged":True}
            else:
                try: after=self._change_row(conn,row,update,actor,"edit",reason,uid())
                except sqlite3.IntegrityError: raise Problem("DECISION_CONFLICT","同主题已有有效决策")
                result={"memory_id":memory_id,"status":after["status"],"version":after["version"]}
            self._save_idempotent(conn,actor,"edit_memory",key,hashed,result)
            self._log(conn,actor,"edit_memory",memory_id)
            return result

    def supersede(self,actor,old_id,expected_version,new_id,expected_new_version,reason,key):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只能由本地管理端批准替代",403)
        request={"old_id":old_id,"old_version":expected_version,"new_id":new_id,"new_version":expected_new_version,"reason":reason}
        if not reason.strip() or old_id==new_id: raise Problem("VALIDATION_ERROR","替代原因或对象无效",422)
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"supersede",key,request)
            if replay: return replay
            old=self._memory(conn,old_id,actor); new=self._memory(conn,new_id,actor)
            self._check_version(old,expected_version); self._check_version(new,expected_new_version)
            if old["status"]!="Active" or new["status"]!="Candidate" or old["project_id"]!=new["project_id"] or old["type"]!=new["type"] or (old["type"]=="Decision" and old["decision_key"]!=new["decision_key"]) or new["confidence"]=="Inferred" or new["superseded_by"]:
                raise Problem("INVALID_TRANSITION","这两条记忆不能建立替代关系")
            self._project(conn,old["project_id"],True)
            op=uid()
            old_after=self._change_row(conn,old,{"status":"Superseded","superseded_by":new_id},actor,"supersede",reason,op)
            try: new_after=self._change_row(conn,new,{"status":"Active"},actor,"activate_replacement",reason,op)
            except sqlite3.IntegrityError: raise Problem("DECISION_CONFLICT","同主题已有有效决策")
            result={"old_id":old_id,"new_id":new_id,"old_status":old_after["status"],"new_status":new_after["status"],"operation_id":op}
            self._save_idempotent(conn,actor,"supersede",key,hashed,result)
            self._log(conn,actor,"supersede",old_id)
            return result

    def transition(self,actor,memory_id,expected_version,action,reason,key):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可执行此操作",403)
        if action not in ("archive","reopen","delete","restore") or not reason.strip(): raise Problem("VALIDATION_ERROR","操作或原因无效",422)
        request={"id":memory_id,"version":expected_version,"action":action,"reason":reason}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"transition",key,request)
            if replay: return replay
            row=self._memory(conn,memory_id,actor,True); self._check_version(row,expected_version)
            state=row["status"]
            if action=="archive" and state in ("Candidate","Active"): changes={"status":"Archived"}
            elif action=="reopen" and state=="Archived": changes={"status":"Candidate"}
            elif action=="delete" and state!="Deleted": changes={"status":"Deleted","deleted_from_status":state}
            elif action=="restore" and state=="Deleted":
                prior=row["deleted_from_status"]
                changes={"status":"Superseded" if row["superseded_by"] or prior=="Superseded" else "Archived" if prior=="Archived" else "Candidate","deleted_from_status":None}
            else: raise Problem("INVALID_TRANSITION","不允许的状态变化")
            after=self._change_row(conn,row,changes,actor,action,reason,uid())
            if action in ("delete", "archive"):
                # A later replay must not resurrect an automatic record the
                # user removed, even from a different client or session.
                conn.execute("INSERT OR IGNORE INTO automation_suppressions(project_id,evidence_hash,created_at) SELECT ?,evidence_hash,? FROM automation_memory_links WHERE memory_id=?", (row["project_id"],now(),memory_id))
            result={"memory_id":memory_id,"status":after["status"],"version":after["version"]}
            self._save_idempotent(conn,actor,"transition",key,hashed,result)
            self._log(conn,actor,action,memory_id)
            return result

    def list_memories(self, actor, project_id=None, status="Active", type=None, limit=50, offset=0):
        if status not in STATUSES or (actor["kind"]!="admin" and status=="Deleted") or type is not None and type not in TYPES:
            raise Problem("VALIDATION_ERROR","筛选条件无效",422)
        if not 1<=limit<=100 or offset<0: raise Problem("VALIDATION_ERROR","分页条件无效",422)
        projects=[project_id] if project_id else None if actor.get("library") else ([r["project_id"] for r in self.list_projects(actor)] if actor["kind"]=="admin" else list(actor["projects"]))
        if projects is not None:
            for pid in projects: self._scope(actor,pid)
            if not projects: return []
        sql="SELECT m.* FROM memories m JOIN projects p ON p.project_id=m.project_id JOIN sources s ON s.source_id=m.source_id WHERE m.status=?"
        args=[status]
        if projects is not None:
            sql+=" AND m.project_id IN (SELECT value FROM json_each(?))";args.append(canonical(projects))
        if status=="Active": sql+=" AND p.status='active'"
        if type: sql+=" AND m.type=?"; args.append(type)
        with self.db.read() as conn:
            gate,policy_args=self._read_filter(conn,actor)
            sql+=" AND "+gate;args.extend(policy_args)
            sql+=" ORDER BY m.updated_at DESC,m.memory_id LIMIT ? OFFSET ?";args.extend([limit,offset])
            return [dict(row) for row in conn.execute(sql,args)]

    def source_qualifiers(self, memory):
        """Hydrate qualifiers only from this already-authorized memory's source."""
        from .automation_text import synthetic_qualifiers
        quote=memory.get("source_quote","")
        try:
            with self.db.read() as conn:
                source=conn.execute("SELECT content,project_id,external_ref FROM sources WHERE source_id=?",(memory["source_id"],)).fetchone()
                if not source or source["project_id"]!=memory["project_id"]:
                    return {}
                flags=synthetic_qualifiers(source["content"],quote)
                if source["external_ref"] and source["external_ref"].startswith("tool-observation-v1:") and source["content"]==quote:
                    from .tool_observations import stored_metadata
                    meta=stored_metadata(source["content"])
                    link=conn.execute("SELECT event_id,evidence_hash FROM automation_memory_links WHERE memory_id=? AND source_role='tool' LIMIT 1",(memory["memory_id"],)).fetchone()
                    if meta and link and link["evidence_hash"]==digest(quote) and source["external_ref"].startswith("tool-observation-v1:"+link["event_id"]+":"):
                        body=source["content"].partition("\n")[2]
                        prefix="工具 "+meta["name"]+" 执行观察：\n"
                        raw=body[len(prefix):] if body.startswith(prefix) else body
                        flags={**synthetic_qualifiers(raw,raw),**flags,"tool_observation":meta}
                if flags:
                    return {**flags,"source_id":memory["source_id"]}
                rows=conn.execute("SELECT l.evidence_hash,l.source_role,l.event_id,e.payload_json FROM automation_memory_links l JOIN automation_events e USING(event_id) WHERE l.memory_id=? LIMIT 2",(memory["memory_id"],)).fetchall()
            for row in rows:
                if row["source_role"]!="user" or row["evidence_hash"]!=digest(quote):
                    continue
                payload=json.loads(row["payload_json"])
                if payload.get("scope",{}).get("project_id")!=memory["project_id"]:
                    continue
                for message in payload.get("messages",[]):
                    if message.get("role")!="user" or source["external_ref"]!=row["event_id"]+":"+message.get("id",""):
                        continue
                    flags=synthetic_qualifiers(message.get("text",""),quote)
                    if flags:
                        return {**flags,"event_id":row["event_id"],"message_id":message.get("id")}
        except (sqlite3.OperationalError,ValueError,KeyError,TypeError):
            pass
        return {}

    def get_memory(self,actor,memory_id,include_history=False,source_offset=0):
        if source_offset<0: raise Problem("VALIDATION_ERROR","来源分页无效",422)
        with self.db.read() as conn:
            row=self._memory(conn,memory_id,actor,actor["kind"]=="admin")
            source=self._source(conn,row["source_id"],row["project_id"],actor)
            qualifiers=self.source_qualifiers(dict(row))
            original_quote=row["source_quote"]
            if actor["kind"]!="admin" and len(row["source_quote"])>200:
                row=dict(row)
                row["source_quote"]=row["source_quote"][:200]
                row["source_quote_truncated"]=True
            output={"memory":row,"source":{key:source[key] for key in ("source_id","project_id","title","source_type","origin_app","content_hash","visibility","created_at")},"source_page":source["content"][source_offset:source_offset+8000],"next_source_offset":source_offset+8000 if source_offset+8000<len(source["content"]) else None}
            output["source_qualifiers"]=qualifiers
            history_truncated=False
            history=[]
            if include_history:
                for rev in conn.execute("SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY version DESC LIMIT 51",(memory_id,)):
                    item=dict(rev)
                    if actor["kind"]=="admin":
                        item["before_snapshot"]=json.loads(item["before_snapshot"]) if item["before_snapshot"] else None
                        item["after_snapshot"]=json.loads(item["after_snapshot"])
                    else:
                        item.pop("before_snapshot"); item.pop("after_snapshot")
                    history.append(item)
                history_truncated=len(history)>50
                output["revisions"]=history[:50]
            summary=conn.execute("SELECT count(*) n,max(version) latest_version FROM memory_revisions WHERE memory_id=?",(memory_id,)).fetchone()
            roles={link["source_role"] for link in conn.execute("SELECT source_role,evidence_hash FROM automation_memory_links WHERE memory_id=?",(memory_id,)) if link["evidence_hash"]==digest(original_quote)}
            reporter=next(iter(roles)) if len(roles)==1 and roles<= {"user","tool"} else "unspecified"
            output["assertion_origin"]={"reporter":reporter,"basis":"matching_capture_evidence" if reporter!="unspecified" else "source_text_only"}
            output["revision_summary"]={"scope":"this_memory_record_only","stored_revision_rows":summary["n"],"latest_stored_revision_version":summary["latest_version"],"other_plan_records_enumerated":False}
            output["evidence_scope"]=detail_scope(source_offset,len(source["content"]),include_history,min(len(history),50),history_truncated)
            output["answer_evidence"]=answer_evidence(output)
            return output if actor["kind"]=="admin" else without_audit_accounts(output)

    def get_source(self,actor,source_id,offset=0):
        if offset<0: raise Problem("VALIDATION_ERROR","来源分页无效",422)
        with self.db.read() as conn:
            row=conn.execute("SELECT * FROM sources WHERE source_id=?",(source_id,)).fetchone()
            if not row: raise Problem("NOT_FOUND","来源不可用",404)
            row=self._source(conn,source_id,row["project_id"],actor)
            self._scope(actor,row["project_id"])
            content=row.pop("content")
            row["content_page"]=content[offset:offset+8000]
            row["next_offset"]=offset+8000 if offset+8000<len(content) else None
            return row

    def search(self,actor,query,project_id=None,project_ids=None,include_global=True,limit=5,history_status=None,time_budget_ms=None,library=False):
        if not isinstance(query,str) or len(query)>500 or not query.strip() or not 1<=limit<=20:
            raise Problem("VALIDATION_ERROR","搜索词或返回数量无效",422)
        if project_id and project_ids or (library and (project_id or project_ids)):
            raise Problem("VALIDATION_ERROR","项目参数无效",422)
        preferred = actor.get("current_project_id") if library else project_id
        projects = None if library else list(dict.fromkeys(project_ids)) if project_ids is not None else [project_id or "global"]
        if projects is not None:
            if not projects or len(projects)>20:
                raise Problem("VALIDATION_ERROR","跨项目范围无效",422)
            if project_id and include_global and project_id!="global" and (actor["kind"]=="admin" or actor.get("library") or "global" in actor["projects"]):
                projects.append("global")
            for pid in projects:
                self._scope(actor,pid)
            preferred = preferred or projects[0]
        if history_status is not None and history_status not in {"Superseded","Archived","Candidate"}:
            raise Problem("VALIDATION_ERROR","历史状态无效",422)
        # Grammatical glue must not make the OR fallback match unrelated rows.
        # Keep the stored tokenizer unchanged; this only narrows query terms.
        terms=[term for term in dict.fromkeys(tokenize(query)) if term not in QUERY_STOPWORDS]
        if not terms:
            raise Problem("VALIDATION_ERROR","搜索词没有可检索内容",422)
        with self.db.read() as conn:
            if time_budget_ms is not None:
                budget=max(1,int(time_budget_ms));deadline=time.monotonic()+budget/1000
                conn.execute("PRAGMA busy_timeout="+str(budget))
                conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            if projects is not None:
                for pid in projects:self._project(conn,pid)
            gate,args=self._read_filter(conn,actor)
            plan_states = plans.query_states(query) if history_status is None else None
            plan_gate,plan_args=plans.filter_gate("m",plan_states)
            gate += " AND "+plan_gate
            args += plan_args
            scope="m.project_id IN (SELECT value FROM json_each(?)) AND " if projects is not None else ""
            scope_args=[canonical(projects)] if projects is not None else []
            order=" ORDER BY CASE WHEN m.project_id=? THEN 0 WHEN m.project_id IN ('global','personal-sessions') THEN 1 ELSE 2 END,"
            if history_status:
                match="("+" OR ".join("(instr(lower(m.content),?)>0 OR instr(lower(s.title),?)>0)" for t in terms)+")"
                sql="SELECT m.*,s.title source_title,p.name project_name FROM memories m JOIN sources s USING(source_id) JOIN projects p ON p.project_id=m.project_id WHERE "+scope+gate+" AND m.status=? AND "+match+order+"m.updated_at DESC,m.memory_id LIMIT 512"
                values=[dict(r) for r in conn.execute(sql,[*scope_args,*args,history_status,*[x for t in terms for x in (t,t)],preferred])]
                mode="historical"
            else:
                def matches(joiner):
                    expression=joiner.join('"'+term.replace('"','""')+'"' for term in terms)
                    sql="SELECT m.*,s.title source_title,p.name project_name,bm25(memory_fts) rank FROM memory_fts JOIN memories m ON m.memory_id=memory_fts.memory_id JOIN sources s ON s.source_id=m.source_id JOIN projects p ON p.project_id=m.project_id WHERE memory_fts MATCH ? AND "+scope+gate+" AND m.status='Active' AND p.status='active'"+order+"rank,CASE m.confidence WHEN 'Confirmed' THEN 0 WHEN 'Reported' THEN 1 ELSE 2 END,m.memory_id LIMIT 512"
                    return [dict(r) for r in conn.execute(sql,[expression,*scope_args,*args,preferred])]
                values=matches(" AND ");mode="all_terms"
                if not values and len(terms)>1:
                    values=matches(" OR ");mode="any_terms"
                if not values:
                    # A query can wrap a stored phrase without a lexical match
                    # ("发布说明" may segment as "发布"/"说明"). Match spaced or
                    # contiguous Chinese token pairs from the original prompt;
                    # unrelated prompts stay empty rather than injecting rules.
                    compact=re.sub(r"\s+", "", query.lower())
                    pairs=[]
                    for position,term in enumerate(terms[:-1]):
                        pair=term+terms[position+1]
                        if (len(pair)>=4 and not pair.isascii() and pair in compact
                                and pair not in pairs):
                            pairs.append(pair)
                    if pairs:
                        pairs=pairs[:24]
                        match=" OR ".join("(instr(replace(lower(m.content),' ',''),?)>0 OR instr(replace(lower(s.title),' ',''),?)>0)" for _ in pairs)
                        sql="SELECT m.*,s.title source_title,p.name project_name FROM memories m JOIN sources s USING(source_id) JOIN projects p ON p.project_id=m.project_id WHERE "+scope+gate+" AND m.status='Active' AND p.status='active' AND ("+match+")"+order+"m.updated_at DESC,m.memory_id LIMIT 512"
                        values=[dict(r) for r in conn.execute(sql,[*scope_args,*args,*[x for pair in pairs for x in (pair,pair)],preferred])]
                        if values:
                            mode="contiguous_fallback"
            if plan_states:
                placeholders=",".join("?" for _ in plan_states)
                sql="SELECT m.*,s.title source_title,p.name project_name FROM memories m JOIN sources s USING(source_id) JOIN projects p USING(project_id) WHERE "+scope+gate+" AND m.status='Active' AND p.status='active' AND m.record_kind='plan' AND m.plan_status IN ("+placeholders+")"+order+"m.updated_at DESC,m.memory_id LIMIT 32"
                matched=[dict(r) for r in conn.execute(sql,[*scope_args,*args,*plan_states,preferred])]
                ids={r["memory_id"] for r in matched}
                values=matched+[r for r in values if r["memory_id"] not in ids]
                if matched:mode="plan_state"
            result=[]
            for row in values[:limit]:
                item={key:row[key] for key in ("memory_id","type","status","confidence","project_id","project_name","version","source_id","source_title","updated_at","superseded_by","record_kind","plan_status","plan_key")}
                item.update(content_excerpt=row["content"][:600],source_excerpt=row["source_quote"][:200],match_mode=mode,truncated=len(row["content"])>600 or len(row["source_quote"])>200)
                result.append(item)
            return {"items":result,"total_matches":len(values),"truncated":len(values)>limit,"candidate_count":len(values),"candidate_limit":512,"plan_states":plan_states}

    def submit_change(self,actor,memory_id,action,expected_version,payload,reason,key):
        if action not in ("edit","supersede") or not reason.strip() or not isinstance(payload,dict): raise Problem("VALIDATION_ERROR","修改建议无效",422)
        request={"id":memory_id,"action":action,"version":expected_version,"payload":payload,"reason":reason}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"submit_change",key,request)
            if replay:
                self._memory(conn,memory_id,actor)
                return replay
            target=self._memory(conn,memory_id,actor)
            self._scope(actor,target["project_id"],True)
            self._check_version(target,expected_version)
            if target["status"]!="Active": raise Problem("INVALID_TRANSITION","只能对有效记忆提出修改")
            if action=="edit" and (not payload or set(payload)-{"content","confidence","decision_key","source_id","source_span"}): raise Problem("VALIDATION_ERROR","修改字段无效",422)
            if action=="supersede" and set(payload)!={"new_id","new_version"}: raise Problem("VALIDATION_ERROR","替代建议需要新候选 ID 和版本",422)
            ident=uid()
            conn.execute("INSERT INTO change_requests(request_id,target_id,action,base_version,proposed_payload,reason,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?)",(ident,memory_id,action,expected_version,canonical(payload),reason,actor["id"],now()))
            result={"change_request_id":ident,"status":"pending_review","memory_id":memory_id}
            self._save_idempotent(conn,actor,"submit_change",key,hashed,result)
            self._log(conn,actor,"submit_change",memory_id)
            return result

    def list_changes(self,actor,status="pending"):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可审核",403)
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM change_requests WHERE status=? ORDER BY created_at",(status,))]

    def review_change(self,actor,request_id,expected_version,approve,key):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可审核",403)
        request={"id":request_id,"version":expected_version,"approve":approve}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"review_change",key,request)
            if replay: return replay
            proposal=conn.execute("SELECT * FROM change_requests WHERE request_id=?",(request_id,)).fetchone()
            if not proposal: raise Problem("NOT_FOUND","建议不存在",404)
            proposal=dict(proposal)
            if proposal["status"]!="pending" or proposal["version"]!=expected_version: raise Problem("VERSION_CONFLICT","建议已变化")
            target=self._memory(conn,proposal["target_id"],actor)
            if target["version"]!=proposal["base_version"]:
                conn.execute("UPDATE change_requests SET status='conflict',version=version+1,reviewed_at=? WHERE request_id=?",(now(),request_id))
                result={"change_request_id":request_id,"status":"conflict"}
            elif not approve:
                conn.execute("UPDATE change_requests SET status='rejected',version=version+1,reviewed_at=? WHERE request_id=?",(now(),request_id))
                result={"change_request_id":request_id,"status":"rejected"}
            else:
                payload=json.loads(proposal["proposed_payload"])
                if proposal["action"]=="edit":
                    content=payload.get("content",target["content"]); confidence=payload.get("confidence",target["confidence"]); dkey=payload.get("decision_key",target["decision_key"])
                    if isinstance(dkey,str): dkey=dkey.strip()
                    self._validate_memory(content,target["type"],confidence,dkey)
                    if confidence=="Inferred": raise Problem("VALIDATION_ERROR","推断不能直接生效",422)
                    source=self._source(conn,payload.get("source_id",target["source_id"]),target["project_id"],actor)
                    if "source_id" in payload and "source_span" not in payload: raise Problem("VALIDATION_ERROR","更换来源时需要位置",422)
                    start,end,quote=self._span(source,payload.get("source_span",{"start":target["source_start"],"end":target["source_end"],"quote":target["source_quote"]}))
                    update={"content":content,"confidence":confidence,"decision_key":dkey,"source_id":source["source_id"],"source_start":start,"source_end":end,"source_quote":quote}
                    if any(target[k]!=v for k,v in update.items()): self._change_row(conn,target,update,actor,"reviewed_edit",proposal["reason"],uid())
                else:
                    new=self._memory(conn,payload["new_id"],actor)
                    self._check_version(new,payload["new_version"])
                    if new["status"]!="Candidate" or new["project_id"]!=target["project_id"] or new["type"]!=target["type"] or new["confidence"]=="Inferred" or (new["type"]=="Decision" and new["decision_key"]!=target["decision_key"]):
                        raise Problem("INVALID_TRANSITION","新候选不能替代当前记忆")
                    op=uid()
                    self._change_row(conn,target,{"status":"Superseded","superseded_by":new["memory_id"]},actor,"supersede",proposal["reason"],op)
                    self._change_row(conn,new,{"status":"Active"},actor,"activate_replacement",proposal["reason"],op)
                conn.execute("UPDATE change_requests SET status='approved',version=version+1,reviewed_at=? WHERE request_id=?",(now(),request_id))
                result={"change_request_id":request_id,"status":"approved"}
            self._save_idempotent(conn,actor,"review_change",key,hashed,result)
            self._log(conn,actor,"review_change",request_id)
            return result

    def set_source_visibility(self,actor,source_id,visibility,expected_version,key):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可修改来源",403)
        if visibility not in ("agent_allowed","local_only"): raise Problem("VALIDATION_ERROR","可见性无效",422)
        request={"id":source_id,"visibility":visibility,"version":expected_version}
        with self.db.tx() as conn:
            replay,hashed=self._idempotent(conn,actor,"source_visibility",key,request)
            if replay: return replay
            row=conn.execute("SELECT * FROM sources WHERE source_id=?",(source_id,)).fetchone()
            if not row: raise Problem("NOT_FOUND","来源不存在",404)
            if row["version"]!=expected_version: raise Problem("VERSION_CONFLICT","来源版本已变化")
            conn.execute("UPDATE sources SET visibility=?,version=version+1 WHERE source_id=?",(visibility,source_id))
            conn.execute("DELETE FROM memory_vectors WHERE memory_id IN (SELECT memory_id FROM memories WHERE source_id=?)",(source_id,))
            result={"source_id":source_id,"visibility":visibility,"version":expected_version+1}
            self._save_idempotent(conn,actor,"source_visibility",key,hashed,result)
            self._log(conn,actor,"source_visibility",source_id)
            return result

    def create_client(self,actor,name,project_ids,can_write):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可创建客户端",403)
        if not name.strip() or len(name)>100 or not project_ids: raise Problem("VALIDATION_ERROR","客户端信息无效",422)
        with self.db.tx() as conn:
            for pid in project_ids: self._project(conn,pid)
            token=secrets.token_urlsafe(32); ident=uid()
            conn.execute("INSERT INTO client_credentials VALUES (?,?,?,?,?,?,?)",(ident,name,digest(token),canonical(list(dict.fromkeys(project_ids))),int(can_write),0,now()))
            self._log(conn,actor,"create_client",ident)
            return {"client_id":ident,"name":name,"token":token,"project_ids":project_ids,"can_write":can_write}

    def revoke_client(self,actor,client_id):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可撤销客户端",403)
        with self.db.tx() as conn:
            if conn.execute("UPDATE client_credentials SET revoked=1 WHERE client_id=? AND revoked=0",(client_id,)).rowcount!=1:
                raise Problem("NOT_FOUND","客户端不存在或已撤销",404)
            self._log(conn,actor,"revoke_client",client_id)
            return {"client_id":client_id,"revoked":True}

    def verify_client(self,token):
        with self.db.read() as conn:
            row=conn.execute("SELECT * FROM client_credentials WHERE token_hash=? AND revoked=0",(digest(token),)).fetchone()
            if not row: return None
            return {"kind":"client","id":row["client_id"],"projects":json.loads(row["project_ids"]),"can_write":bool(row["can_write"])}

    def list_clients(self,actor):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可查看客户端",403)
        with self.db.read() as conn:
            return [{k:r[k] for k in ("client_id","name","project_ids","can_write","revoked","created_at")} for r in conn.execute("SELECT * FROM client_credentials ORDER BY created_at DESC")]

    def backup(self,actor,backup_dir:Path):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可备份",403)
        backup_dir.mkdir(parents=True,exist_ok=True)
        ident=uid(); snapshot=backup_dir/(ident+".db"); package=backup_dir/(ident+".zip")
        try:
            with self.db.read() as source:
                target=sqlite3.connect(snapshot)
                try: source.backup(target)
                finally: target.close()
            data=snapshot.read_bytes()
            check=sqlite3.connect(snapshot)
            from .db import load_vector_extension
            load_vector_extension(check)
            try:
                integrity=check.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity!="ok": raise Problem("STORAGE_FAILURE","备份校验失败",503)
                counts={table:check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                    "projects","sources","memories","memory_revisions","change_requests",
                    "workspace_aliases","workspace_roots","source_scopes","session_scopes",
                    "automation_event_identity","automation_job_history")}
            finally:
                check.close()
            manifest={"format_version":1,"app_version":APP_VERSION,"schema_version":SCHEMA_VERSION,"tokenizer_version":TOKENIZER_VERSION,"created_at":now(),"sha256":hashlib.sha256(data).hexdigest(),"counts":counts}
            with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json",canonical(manifest)); archive.write(snapshot,"memory.db")
            with self.db.tx() as conn: self._log(conn,actor,"backup",ident)
            return {"backup_id":ident,"path":str(package),"manifest":manifest}
        finally:
            snapshot.unlink(missing_ok=True)

    def export_json(self,actor,export_dir:Path):
        if actor["kind"]!="admin": raise Problem("FORBIDDEN","只有管理员可导出",403)
        export_dir.mkdir(parents=True,exist_ok=True)
        ident=uid(); path=export_dir/(ident+".json")
        with self.db.read() as conn:
            result={"format_version":1,"exported_at":now(),"schema_version":SCHEMA_VERSION}
            for table in ("projects","sources","memories","memory_revisions","change_requests","operation_log","workspace_bindings","workspace_aliases","workspace_roots","source_scopes","session_scopes","automation_events","automation_event_identity","automation_jobs","automation_job_history","automation_memory_links","automation_suppressions","task_checkpoints","task_checkpoint_sessions","task_checkpoint_grants","task_checkpoint_annotations","automatic_decisions"):
                result[table]=[dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            result["automation_clients"]=[{k:r[k] for k in ("client_id","app","enabled","revoked","created_at","last_read_at","last_capture_at","access_mode","excluded_roots_json","purpose")} for r in conn.execute("SELECT * FROM automation_clients")]
        path.write_text(canonical(result),encoding="utf-8")
        with self.db.tx() as conn: self._log(conn,actor,"export",ident)
        return {"export_id":ident,"path":str(path),"counts":{k:len(result[k]) for k in result if isinstance(result[k],list)}}
