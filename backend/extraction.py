from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .service import MemoryService, Problem, canonical, now, uid

MODEL_ID = "deepseek-flash"
EMBED_MODEL_ID = "bge-m3:latest"
PROMPT_VERSION = "deepseek-extract-v3"
SCHEMA_VERSION = 2
MAX_SELECTION = 6000
FINAL_MARKER = re.compile(r"最终决定|决定|确定|拍板|采用|同意|选择|最终结论")
ITEM_KEYS = {"content", "type", "confidence", "decision_key", "quote"}
MEMORY_TYPES = {"Fact", "Decision", "Preference", "Rule", "Experience", "OpenQuestion"}

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"items": {"type": "array", "maxItems": 5, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "content": {"type": "string"},
            "type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
            "confidence": {"type": "string", "enum": ["Reported", "Inferred"]},
            "decision_key": {"type": ["string", "null"]},
            "quote": {"type": "string"},
        },
        "required": sorted(ITEM_KEYS),
    }}}, "required": ["items"],
}


class OllamaClient:
    """Only the local loopback Ollama API is reachable through this adapter."""

    def __init__(self, base_url="http://127.0.0.1:11434"):
        if base_url != "http://127.0.0.1:11434":
            raise ValueError("P1 仅允许本机 Ollama 地址")
        self.base_url = base_url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path, payload=None, timeout=10):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"}, method="GET" if data is None else "POST")
        with self.opener.open(req, timeout=timeout) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("模型响应过大")
        return json.loads(raw)

    def model_digest(self, model_id, timeout=10):
        for item in self._request("/api/tags",timeout=timeout).get("models", []):
            if item.get("name") == model_id:
                digest = item.get("digest")
                if isinstance(digest, str) and len(digest) == 64:
                    return digest
        raise ValueError("本地模型未安装")

    def embed(self, texts, timeout=180):
        output = self._request("/api/embed", {"model": EMBED_MODEL_ID, "input": texts, "truncate": False, "options": {"num_gpu": 0}}, timeout=timeout)
        vectors = output.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("向量模型返回数量不正确")
        return vectors


def validate_output(raw, selected_text):
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("模型输出不是有效 JSON")
    if not isinstance(result, dict) or set(result) != {"items"} or not isinstance(result["items"], list) or len(result["items"]) > 5:
        raise ValueError("模型输出结构无效")
    valid = []
    for item in result["items"]:
        if not isinstance(item, dict) or set(item) != ITEM_KEYS:
            raise ValueError("候选字段不完整或多出字段")
        content, kind, confidence = item["content"], item["type"], item["confidence"]
        key, quote = item["decision_key"], item["quote"]
        if not isinstance(content, str) or not content.strip() or len(content) > 2000 or kind not in MEMORY_TYPES or confidence not in {"Reported", "Inferred"}:
            raise ValueError("候选内容或证据等级无效")
        if not isinstance(quote, str) or not quote.strip() or quote not in selected_text:
            raise ValueError("模型引文与原文不一致")
        start = selected_text.index(quote)
        normalized = dict(item)
        normalized["content"] = content.strip()
        normalized["start"], normalized["end"] = start, start + len(quote)
        if kind == "Decision" and (confidence != "Reported" or not FINAL_MARKER.search(quote)):
            normalized["type"] = "OpenQuestion"
            normalized["confidence"] = "Inferred"
            normalized["decision_key"] = None
        elif kind == "Decision" and (not isinstance(key, str) or not key.strip() or len(key) > 120):
            raise ValueError("决策缺少主题")
        elif kind != "Decision":
            normalized["decision_key"] = None
        valid.append(normalized)
    return valid


class ExtractionService:
    def __init__(self, service: MemoryService, model=None):
        self.service = service
        from .manual_provider import ManualModel
        self.model = model or ManualModel(service.db.path.parent)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="personal-memory-extract")
        with self.service.db.tx() as conn:
            conn.execute("UPDATE extraction_jobs SET status='interrupted',error_code='SERVICE_RESTARTED',updated_at=? WHERE status IN ('pending','running')", (now(),))

    def preview(self, actor, source_id, start, end):
        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "只有本地管理端可提炼", 403)
        with self.service.db.read() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row:
                raise Problem("NOT_FOUND", "来源不存在", 404)
            source = self.service._source(conn, source_id, row["project_id"], actor)
            if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(source["content"]) or end - start > MAX_SELECTION:
                raise Problem("VALIDATION_ERROR", "请选择 1–6000 字符的原文片段", 422)
            selected = source["content"][start:end]
            return {"source_id": source_id, "project_id": source["project_id"], "title": source["title"], "start": start, "end": end, "text": selected, "text_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(), "provider": getattr(self.model,"describe",lambda:{"provider":"DeepSeek"})()["provider"], "model_id": MODEL_ID}

    def start(self, actor, source_id, start, end, expected_sha, confirmed, key):
        if confirmed is not True:
            raise Problem("CONSENT_REQUIRED", "请先查看最终发送给DeepSeek的原文", 422)
        preview = self.preview(actor, source_id, start, end)
        if preview["text_sha256"] != expected_sha:
            raise Problem("VERSION_CONFLICT", "原文与预览不一致", 409)
        request = {"source_id": source_id, "start": start, "end": end, "text_sha256": expected_sha, "model_id": MODEL_ID}
        with self.service.db.tx() as conn:
            replay, hashed = self.service._idempotent(conn, actor, "start_extraction", key, request)
            if replay:
                return replay
            job_id = uid()
            conn.execute("INSERT INTO extraction_jobs(job_id,source_id,project_id,selection_start,selection_end,text_sha256,provider,model_id,model_digest,prompt_version,response_schema_version,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id,source_id,preview["project_id"],start,end,expected_sha,"DeepSeek",MODEL_ID,"pending",PROMPT_VERSION,SCHEMA_VERSION,"pending",now(),now()))
            result = {"job_id": job_id, "status": "pending"}
            self.service._save_idempotent(conn, actor, "start_extraction", key, hashed, result)
            self.service._log(conn, actor, "start_extraction", job_id)
        self.pool.submit(self.process, job_id)
        return result

    def get(self, actor, job_id):
        if actor["kind"] != "admin":
            raise Problem("FORBIDDEN", "只有本地管理端可查看提炼任务", 403)
        with self.service.db.read() as conn:
            row = conn.execute("SELECT * FROM extraction_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise Problem("NOT_FOUND", "提炼任务不存在", 404)
            value = dict(row)
            value["candidate_ids"] = json.loads(value["candidate_ids"])
            value["warnings"] = json.loads(value["warnings"])
            return value

    def process(self, job_id):
        try:
            with self.service.db.tx() as conn:
                job = conn.execute("SELECT * FROM extraction_jobs WHERE job_id=? AND status='pending'", (job_id,)).fetchone()
                if not job:
                    return
                source = conn.execute("SELECT * FROM sources WHERE source_id=?", (job["source_id"],)).fetchone()
                selected = source["content"][job["selection_start"]:job["selection_end"]]
                if hashlib.sha256(selected.encode("utf-8")).hexdigest() != job["text_sha256"]:
                    raise ValueError("原文哈希变化")
                conn.execute("UPDATE extraction_jobs SET status='running',updated_at=? WHERE job_id=?", (now(), job_id))
            digest = self.model.model_digest(MODEL_ID)
            repair = None
            for attempt in range(2):
                raw = self.model.generate(selected, repair)
                try:
                    items = validate_output(raw, selected)
                    break
                except ValueError as error:
                    if attempt == 1:
                        raise
                    repair = str(error)
            self._commit_candidates(job_id, digest, items, selected)
        except (urllib.error.URLError, TimeoutError):
            self._fail(job_id, "MODEL_UNAVAILABLE")
        except (ValueError, KeyError, TypeError):
            self._fail(job_id, "MODEL_OUTPUT_INVALID")
        except Exception:
            self._fail(job_id, "STORAGE_FAILURE")

    def _fail(self, job_id, code):
        with self.service.db.tx() as conn:
            conn.execute("UPDATE extraction_jobs SET status='failed',error_code=?,updated_at=? WHERE job_id=? AND status IN ('pending','running')", (code,now(),job_id))

    def _commit_candidates(self, job_id, model_digest, items, selected):
        with self.service.db.tx() as conn:
            job = conn.execute("SELECT * FROM extraction_jobs WHERE job_id=? AND status='running'", (job_id,)).fetchone()
            if not job:
                return
            source = conn.execute("SELECT * FROM sources WHERE source_id=?", (job["source_id"],)).fetchone()
            if not source or source["project_id"] != job["project_id"] or source["content"][job["selection_start"]:job["selection_end"]] != selected:
                raise ValueError("原文已变化")
            ids, warnings = [], []
            actor = {"kind": "extractor", "id": "deepseek:" + model_digest[:12], "projects": [], "can_write": False}
            for item in items:
                start, end = job["selection_start"] + item["start"], job["selection_start"] + item["end"]
                existing = conn.execute("SELECT memory_id FROM memories WHERE project_id=? AND status='Active' AND (content=? OR (type='Decision' AND decision_key=? AND ?='Decision')) LIMIT 1", (job["project_id"],item["content"],item["decision_key"],item["type"])).fetchone()
                ident, stamp = uid(), now()
                conn.execute("INSERT INTO memories(memory_id,project_id,content,type,status,confidence,source_id,source_start,source_end,source_quote,decision_key,created_at,updated_at,created_by,last_modified_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident,job["project_id"],item["content"],item["type"],"Candidate",item["confidence"],job["source_id"],start,end,item["quote"],item["decision_key"],stamp,stamp,actor["id"],actor["id"]))
                row = dict(conn.execute("SELECT * FROM memories WHERE memory_id=?", (ident,)).fetchone())
                self.service._revision(conn,None,row,"extract_candidate","DeepSeek提炼，待人工审核",actor,uid())
                ids.append(ident)
                if existing:
                    warnings.append({"memory_id": ident, "kind": "possible_duplicate_or_conflict", "existing_id": existing["memory_id"]})
            conn.execute("UPDATE extraction_jobs SET status='completed',model_digest=?,candidate_ids=?,warnings=?,updated_at=? WHERE job_id=?", (model_digest,canonical(ids),canonical(warnings),now(),job_id))

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
