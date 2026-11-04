from __future__ import annotations
from . import plans

import math
import struct
import json
import hashlib
import sqlite3
import time
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from .extraction import EMBED_MODEL_ID, OllamaClient
from .service import MemoryService, Problem, now, uid

EMBED_DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"
DIMENSIONS = 1024
QUERY_CACHE_TTL_SECONDS = 300


def pack_vector(values):
    if not isinstance(values, list) or len(values) != DIMENSIONS or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("向量维度或数值无效")
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if not norm:
        raise ValueError("零向量无效")
    return struct.pack(f"<{DIMENSIONS}f", *(float(value) / norm for value in values))


def unpack_vector(raw):
    if len(raw) != DIMENSIONS * 4:
        raise ValueError("向量字节数无效")
    return struct.unpack(f"<{DIMENSIONS}f", raw)


def index_vector(conn, memory_id, raw):
    # Explicit same-transaction insertion: sqlite-vec cannot allocate its first
    # disk chunk from our FK-trigger context on this Windows SQLite build.
    conn.execute("DELETE FROM memory_knn WHERE memory_id=?", (memory_id,))
    conn.execute("INSERT INTO memory_knn(memory_id,vector) VALUES (?,?)", (memory_id,raw))


class SemanticService:
    def __init__(self, service: MemoryService, model=None):
        self.service = service
        self.model = model or OllamaClient()
        self.query_cache = {}
        self.model_ready_at = 0.0
        self.warm_state = "cold"
        self.warm_done = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="personal-memory-vector")
        with service.db.tx() as conn:
            conn.execute("UPDATE vector_jobs SET status='interrupted',error_code='SERVICE_RESTARTED',updated_at=? WHERE status IN ('pending','running')", (now(),))

    def _model_ready(self,timeout=None):
        actual=self.model.model_digest(EMBED_MODEL_ID) if timeout is None else self.model.model_digest(EMBED_MODEL_ID,timeout=timeout)
        if actual != EMBED_DIGEST:
            raise ValueError("向量模型版本与索引不一致")

    def warm(self, timeout=180):
        """Load and verify the local embedding model before the first user read."""
        self.warm_state = "warming"
        try:
            self._model_ready(timeout=timeout)
            with self.service.db.read() as conn:
                indexed = conn.execute("SELECT 1 FROM memory_vectors LIMIT 1").fetchone() is not None
            if indexed:
                vectors = self.model.embed(["记忆预热"], timeout=timeout)
                pack_vector(vectors[0])
            self.model_ready_at = time.monotonic()
            self.warm_state = "ready"
            return True
        except TimeoutError:
            self.warm_state = "timeout"
            return False
        except (ValueError, KeyError, IndexError, urllib.error.URLError):
            self.warm_state = "unavailable"
            return False
        except Exception:
            self.warm_state = "unavailable"
            return False
        finally:
            self.warm_done.set()

    def warm_async(self, timeout=180):
        """Return immediately; wake-up failures never prevent the service starting."""
        future = self.pool.submit(self.warm, timeout)
        future.add_done_callback(lambda _: None)
        return future

    def start_rebuild(self, actor):
        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "只有本地管理端可建立向量索引", 403)
        with self.service.db.tx() as conn:
            existing = conn.execute("SELECT job_id,status FROM vector_jobs WHERE status IN ('pending','running') ORDER BY created_at DESC LIMIT 1").fetchone()
            if existing:
                return dict(existing)
            job_id = uid()
            conn.execute("INSERT INTO vector_jobs(job_id,status,model_id,model_digest,created_at,updated_at) VALUES (?,?,?,?,?,?)", (job_id,"pending",EMBED_MODEL_ID,EMBED_DIGEST,now(),now()))
        self.pool.submit(self.process, job_id)
        return {"job_id": job_id, "status": "pending"}

    def get_job(self, actor, job_id):
        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "只有本地管理端可查看索引任务", 403)
        with self.service.db.read() as conn:
            row = conn.execute("SELECT * FROM vector_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise Problem("NOT_FOUND", "索引任务不存在", 404)
            return dict(row)

    def process(self, job_id):
        try:
            self._model_ready()
            with self.service.db.tx() as conn:
                conn.execute("UPDATE vector_jobs SET status='running',updated_at=? WHERE job_id=? AND status='pending'", (now(), job_id))
                conn.execute("DELETE FROM memory_vectors WHERE model_digest!=? OR memory_id NOT IN (SELECT m.memory_id FROM memories m JOIN sources s ON s.source_id=m.source_id JOIN projects p ON p.project_id=m.project_id WHERE m.status='Active' AND s.visibility='agent_allowed' AND p.status='active')", (EMBED_DIGEST,))
                rows = [dict(row) for row in conn.execute("SELECT m.memory_id,m.version,m.content,s.title FROM memories m JOIN sources s ON s.source_id=m.source_id JOIN projects p ON p.project_id=m.project_id LEFT JOIN memory_vectors v ON v.memory_id=m.memory_id WHERE m.status='Active' AND s.visibility='agent_allowed' AND p.status='active' AND (v.memory_id IS NULL OR v.memory_version!=m.version OR v.model_digest!=?) ORDER BY m.memory_id", (EMBED_DIGEST,))]
                conn.execute("UPDATE vector_jobs SET total_count=?,updated_at=? WHERE job_id=?", (len(rows),now(),job_id))
            indexed = 0
            for offset in range(0, len(rows), 8):
                batch = rows[offset:offset + 8]
                vectors = self.model.embed([row["content"] + "\n" + row["title"] for row in batch])
                if len(vectors) != len(batch):
                    raise ValueError("向量批次数量不匹配")
                with self.service.db.tx() as conn:
                    for row, vector in zip(batch, vectors):
                        current = conn.execute("SELECT m.version,m.status,s.visibility,p.status project_status FROM memories m JOIN sources s ON s.source_id=m.source_id JOIN projects p ON p.project_id=m.project_id WHERE m.memory_id=?", (row["memory_id"],)).fetchone()
                        if not current or current["version"] != row["version"] or current["status"] != "Active" or current["visibility"] != "agent_allowed" or current["project_status"] != "active":
                            continue
                        conn.execute("INSERT INTO memory_vectors(memory_id,memory_version,model_id,model_digest,dimensions,vector,indexed_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET memory_version=excluded.memory_version,model_id=excluded.model_id,model_digest=excluded.model_digest,dimensions=excluded.dimensions,vector=excluded.vector,indexed_at=excluded.indexed_at", (row["memory_id"],row["version"],EMBED_MODEL_ID,EMBED_DIGEST,DIMENSIONS,pack_vector(vector),now()))
                        index_vector(conn, row["memory_id"], pack_vector(vector))
                        indexed += 1
                    conn.execute("UPDATE vector_jobs SET indexed_count=?,updated_at=? WHERE job_id=?", (indexed,now(),job_id))
            with self.service.db.tx() as conn:
                conn.execute("UPDATE vector_jobs SET status='completed',updated_at=? WHERE job_id=?", (now(),job_id))
        except (urllib.error.URLError, TimeoutError):
            self._fail(job_id, "MODEL_UNAVAILABLE")
        except (ValueError, KeyError, TypeError):
            self._fail(job_id, "MODEL_OR_INDEX_INVALID")
        except Exception:
            self._fail(job_id, "INDEX_FAILURE")

    def _fail(self, job_id, code):
        with self.service.db.tx() as conn:
            conn.execute("UPDATE vector_jobs SET status='failed',error_code=?,updated_at=? WHERE job_id=? AND status IN ('pending','running')", (code,now(),job_id))

    def _projects(self, actor, project_id, project_ids, include_global, library=False):
        if library:
            if project_id or project_ids:
                raise Problem("VALIDATION_ERROR", "库级搜索不接受项目限制参数", 422)
            return None
        if project_id and project_ids:
            raise Problem("VALIDATION_ERROR", "项目参数只能选一种", 422)
        if project_ids is not None:
            if not project_ids or len(project_ids) > 20:
                raise Problem("VALIDATION_ERROR", "跨项目范围无效", 422)
            projects = list(dict.fromkeys(project_ids))
        elif project_id:
            projects = [project_id]
            if include_global and project_id != "global" and (actor["kind"] == "admin" or "global" in actor["projects"]):
                projects.append("global")
        else:
            projects = ["global"]
        with self.service.db.read() as conn:
            for ident in projects:
                self.service._scope(actor, ident)
                self.service._project(conn, ident)
        return projects

    def hybrid_search(self, actor, query, project_id=None, project_ids=None, include_global=True, limit=5,time_budget_ms=None,min_similarity=None,library=False):
        deadline=time.monotonic()+time_budget_ms/1000 if time_budget_ms is not None else None
        keyword = self.service.search(actor,query,project_id,project_ids,include_global,20,time_budget_ms=time_budget_ms,library=library)
        from .query_aliases import query_alias
        alias, concept = query_alias(query)
        if alias and not keyword["items"] and (deadline is None or time.monotonic() < deadline):
            budget = max(1, int((deadline - time.monotonic()) * 1000)) if deadline else None
            expanded = self.service.search(actor,alias,project_id,project_ids,include_global,20,time_budget_ms=budget,library=library)
            if expanded["items"]:
                keyword = {**expanded, "query_concept": concept}
        projects = self._projects(actor,project_id,project_ids,include_global,library)
        def fallback(reason):
            limited = dict(keyword)
            limited["items"] = keyword["items"][:limit]
            limited["truncated"] = keyword["total_matches"] > limit
            limited.update(mode="keyword_fallback", reason=reason)
            return limited
        if not 1 <= limit <= 20:
            raise Problem("VALIDATION_ERROR", "返回数量无效", 422)
        with self.service.db.read() as conn:
            if not conn.execute("SELECT 1 FROM memory_vectors LIMIT 1").fetchone():
                return fallback("INDEX_EMPTY")
        try:
            # Startup loading shares the read's existing budget. Waiting three
            # extra seconds here would exceed the host adapter's timeout and
            # lose even the keyword result. Only warm() may mark the model
            # ready after it has actually verified and loaded successfully.
            if self.warm_state == "warming":
                wait = max(0.0, min(3.0, deadline-time.monotonic())) if deadline else 3.0
                if not self.warm_done.wait(timeout=wait):
                    return fallback("MODEL_WARMING")
            # Verify the configured model at least every 30s; cached query
            # embeddings are bounded and tied to this fixed model digest.
            if time.monotonic()-self.model_ready_at>30:
                self._model_ready(timeout=max(.02,deadline-time.monotonic()) if deadline else None)
                self.model_ready_at=time.monotonic()
            if deadline and time.monotonic()>=deadline:
                return fallback("READ_BUDGET_EXCEEDED")
            cache_key=hashlib.sha256(query.encode()).hexdigest()
            cached=self.query_cache.get(cache_key)
            if cached and time.monotonic()-cached[0]<QUERY_CACHE_TTL_SECONDS:
                query_raw=cached[1]
            else:
                vectors=self.model.embed([query],timeout=max(.02,deadline-time.monotonic())) if deadline else self.model.embed([query])
                query_raw=pack_vector(vectors[0])
                if len(self.query_cache)>=128:self.query_cache.pop(next(iter(self.query_cache)))
                self.query_cache[cache_key]=(time.monotonic(),query_raw)
        except TimeoutError:
            return fallback("READ_BUDGET_EXCEEDED" if deadline else "MODEL_TIMEOUT")
        except (ValueError,KeyError,IndexError,urllib.error.URLError):
            return fallback("MODEL_UNAVAILABLE")
        if deadline and time.monotonic() >= deadline:
            return fallback("READ_BUDGET_EXCEEDED")
        try:
            ranked = self._rank_candidates(actor, query_raw, projects, keyword, limit, min_similarity, library, project_id, deadline)
        except sqlite3.OperationalError:
            if deadline and time.monotonic() >= deadline:
                return fallback("READ_BUDGET_EXCEEDED")
            raise
        return fallback("READ_BUDGET_EXCEEDED") if deadline and time.monotonic() >= deadline else ranked

    def _rank_candidates(self, actor, query_raw, projects, keyword, limit, min_similarity, library, project_id, deadline):
        with self.service.db.read() as conn:
            if deadline:
                conn.execute("PRAGMA busy_timeout="+str(max(1,int((deadline-time.monotonic())*1000))))
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            gate,args=self.service._read_filter(conn,actor)
            plan_gate,plan_args=plans.filter_gate("m",keyword.get("plan_states"))
            gate+=" AND "+plan_gate;args+=plan_args
            scope="m.project_id IN (SELECT value FROM json_each(?)) AND " if projects is not None else ""
            scope_args=[json.dumps(projects)] if projects is not None else []
            # The IN subquery is a vec0 prefilter, before native KNN ranking.
            # Only 512 authorized neighbors (no raw vectors) enter Python.
            sql="""WITH candidates AS MATERIALIZED (
                SELECT memory_id,distance FROM memory_knn
                WHERE vector MATCH ? AND k=512 AND memory_id IN (
                    SELECT memory_id FROM (SELECT m.memory_id,
                    row_number() OVER (PARTITION BY m.project_id,m.type,m.content
                        ORDER BY CASE m.confidence WHEN 'Confirmed' THEN 0 WHEN 'Reported' THEN 1 ELSE 2 END,
                        m.updated_at DESC,m.memory_id) AS representative
                    FROM memories m JOIN sources s ON s.source_id=m.source_id
                    JOIN projects p ON p.project_id=m.project_id
                    JOIN memory_vectors v ON v.memory_id=m.memory_id WHERE """+scope+gate+"""
                    AND m.status='Active' AND p.status='active' AND v.memory_version=m.version
                    AND v.model_digest=? AND v.dimensions=?) WHERE representative=1))
                SELECT 1-c.distance similarity,m.memory_id,m.project_id,m.content,m.type,m.status,
                    m.confidence,m.version,m.source_id,m.updated_at,m.superseded_by,m.source_quote,m.record_kind,m.plan_status,m.plan_key,
                    s.title source_title,p.name project_name
                FROM candidates c JOIN memories m USING(memory_id)
                JOIN sources s USING(source_id) JOIN projects p ON p.project_id=m.project_id"""
            rows=[dict(row) for row in conn.execute(sql,[query_raw,*scope_args,*args,EMBED_DIGEST,DIMENSIONS])]
        semantic = [(row["similarity"],row) for row in rows if min_similarity is None or row["similarity"]>=min_similarity]
        semantic.sort(key=lambda pair:(-pair[0],pair[1]["memory_id"]))
        scores, results = {}, {}
        for position, item in enumerate(keyword["items"],1):
            ident = item["memory_id"]
            scores[ident] = scores.get(ident,0) + 1 / (60 + position)
            results[ident] = dict(item)
        # Retain enough vector neighbors for lexical hits below rank 20 to
        # receive their second RRF contribution before pruning final results.
        # Native KNN is already bounded to 512 authorized representatives.
        for position, (_, row) in enumerate(semantic[:50],1):
            ident = row["memory_id"]
            scores[ident] = scores.get(ident,0) + 1 / (60 + position)
            if ident not in results:
                results[ident] = {key:row[key] for key in ("memory_id","type","status","confidence","project_id","project_name","version","source_id","source_title","updated_at","superseded_by","record_kind","plan_status","plan_key")}
                results[ident].update(content_excerpt=row["content"][:600],source_excerpt=row["source_quote"][:200],truncated=len(row["content"])>600 or len(row["source_quote"])>200,match_mode="semantic")
            else:
                results[ident]["match_mode"] = "hybrid"
        preferred = actor.get("current_project_id") if library else project_id or (projects[0] if projects else None)
        ordered = sorted(results, key=lambda ident:(0 if results[ident]["project_id"]==preferred else 1,0 if results[ident]["project_id"] in {"global","personal-sessions"} else 1,0 if keyword.get("plan_states") and results[ident].get("record_kind")=="plan" else 1,-scores[ident],ident))
        return {"items":[results[ident] for ident in ordered[:limit]],"total_matches":len(results),"truncated":len(results)>limit,"mode":"hybrid","model_id":EMBED_MODEL_ID,"semantic_candidates":len(rows),"vector_unpacks":0,"candidate_limit":512,
                "query_concept":keyword.get("query_concept")}

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
