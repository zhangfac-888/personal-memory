from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mcp_server.tools import make_mcp

from .db import Database, SCHEMA_VERSION
from .automation import AutomationService, ContextRequest, LibraryContextRequest, LibraryTurnRequest, TurnRequest
from .extraction import ExtractionService
from .file_import import decode_upload
from .maintenance import mark_data_dir
from .semantic import SemanticService
from .service import MemoryService, Problem, tokenize
from .workspaces import excluded_projects, library_actor
from . import conversation_archive, task_checkpoints, automatic_review, conversation_provenance


def create_app(data_dir: Path, port: int = 8765, extraction_model=None, automation_model=None, frontend_dir:Path|None=None) -> FastAPI:
    data_dir=data_dir.resolve()
    data_dir.mkdir(parents=True,exist_ok=True)
    mark_data_dir(data_dir)
    db=Database(data_dir/"memory.db")
    service=MemoryService(db)
    extraction=ExtractionService(service,extraction_model)
    semantic=SemanticService(service)
    automation=AutomationService(service,semantic,automation_model,data_dir/"automation-outbox")
    discovery_status=data_dir/"discovery-status.json"
    discovery_history=data_dir/"discovery-history.jsonl"
    from .installation import discovery_allowed
    discovery_enabled = discovery_allowed(data_dir, port, Path(__file__).resolve().parent.parent)
    key_file=data_dir/"admin-key.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(32),encoding="ascii")
        try: os.chmod(key_file,0o600)
        except OSError: pass
    admin_key=key_file.read_text(encoding="ascii").strip()
    if not admin_key: raise RuntimeError("Admin key is empty")
    sessions: dict[str,dict] = {}
    failures: dict[str,list[float]] = defaultdict(list)
    admin={"kind":"admin","id":"local-admin","projects":[],"can_write":True}
    mcp=make_mcp(service,semantic)
    mcp_app=mcp.streamable_http_app(streamable_http_path="/")

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with mcp.session_manager.run():
                # Initialize the shared tokenizer before accepting the first bounded hook read.
                tokenize("记忆服务启动预热")
                semantic.warm_async()
                if discovery_enabled:
                    try:
                        import sys
                        sys.path.insert(0,str(Path(__file__).parent.parent/"connect-agent/scripts"))
                        from automatic_install import start_discovery_worker
                        start_discovery_worker(f"http://127.0.0.1:{port}",data_dir)
                    except Exception as exc:
                        discovery_status.write_text(json.dumps({"state":"unavailable","error":type(exc).__name__}),encoding="utf8")
                automation.start()
                yield
        finally:
            automation.close()
            extraction.close()
            semantic.close()

    app=FastAPI(title="Personal Memory",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.automation=automation
    app.state.semantic=semantic

    @app.middleware("http")
    async def local_boundary(request:Request,call_next):
        host=request.headers.get("host","")
        if host not in (f"127.0.0.1:{port}",f"localhost:{port}"):
            return JSONResponse({"code":"FORBIDDEN","message":"无效的本地地址"},status_code=403)
        origin=request.headers.get("origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}",f"http://localhost:{port}"):
            return JSONResponse({"code":"FORBIDDEN","message":"无效的来源地址"},status_code=403)
        if request.url.path.startswith("/automation/"):
            header=request.headers.get("authorization","")
            try:
                if not header.startswith("Bearer "):
                    raise Problem("UNAUTHENTICATED","自动记忆凭据缺失",401)
                request.state.automatic_client=automation.authenticate(header[7:])
                if request.state.automatic_client.get("purpose","automation") != "automation":
                    raise Problem("FORBIDDEN","此凭据仅供只读检索",403)
            except Problem as error:
                return JSONResponse({"code":error.code,"message":error.message},status_code=error.status)
        if request.url.path.startswith("/mcp"):
            header=request.headers.get("authorization","")
            if not header.startswith("Bearer "):
                return JSONResponse({"code":"UNAUTHENTICATED","message":"客户端令牌无效"},status_code=401)
            legacy_actor=service.verify_client(header[7:])
            mcp_actor=legacy_actor
            if not legacy_actor:
                try:
                    automatic=automation.authenticate(header[7:])
                    if automatic.get("purpose","automation") != "mcp":
                        raise Problem("UNAUTHENTICATED","此凭据不可用于主动检索",401)
                    with service.db.read() as conn:
                        excluded=excluded_projects(conn,automatic)
                    mcp_actor=library_actor(automatic,None,excluded)
                except Problem:
                    return JSONResponse({"code":"UNAUTHENTICATED","message":"客户端令牌无效"},status_code=401)
            request.state.mcp_actor=mcp_actor
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/health",) and not (request.url.path=="/api/session" and request.method=="POST"):
            session=sessions.get(request.cookies.get("pm_session",""))
            if not session or session["expires"]<time.time():
                return JSONResponse({"code":"UNAUTHENTICATED","message":"管理会话已失效"},status_code=401)
            request.state.session=session
            if request.method not in ("GET","HEAD") and request.headers.get("x-csrf-token")!=session["csrf"]:
                return JSONResponse({"code":"FORBIDDEN","message":"缺少有效的操作令牌"},status_code=403)
        return await call_next(request)

    @app.exception_handler(Problem)
    async def problem_handler(request:Request,error:Problem):
        return JSONResponse({"code":error.code,"message":error.message,"retryable":error.status==503,"request_id":str(uuid.uuid4())},status_code=error.status)

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_handler(request:Request,error:sqlite3.OperationalError):
        code="DB_BUSY" if "locked" in str(error).lower() or "busy" in str(error).lower() else "STORAGE_FAILURE"
        return JSONResponse({"code":code,"message":"数据库暂时不可用，请稍后重试","retryable":True,"request_id":str(uuid.uuid4())},status_code=503)

    def fields(body:dict,allowed:set[str],required:set[str]=set()):
        if not isinstance(body,dict) or set(body)-allowed or required-set(body):
            raise Problem("VALIDATION_ERROR","请求字段不符合接口要求",422)
        return body

    async def body(request:Request):
        if request.headers.get("content-length") and int(request.headers["content-length"])>2*1024*1024:
            raise Problem("VALIDATION_ERROR","请求体过大",422)
        try: value=await request.json()
        except (json.JSONDecodeError,UnicodeDecodeError): raise Problem("VALIDATION_ERROR","JSON 内容无效",422)
        if not isinstance(value,dict): raise Problem("VALIDATION_ERROR","请求体必须是对象",422)
        return value

    def key(request:Request):
        value=request.headers.get("idempotency-key","")
        if not value: raise Problem("VALIDATION_ERROR","缺少 Idempotency-Key",422)
        return value

    @app.get("/api/health")
    def health(): return {"status":"ok"}

    @app.post("/api/session")
    async def login(request:Request):
        payload=fields(await body(request),{"key"},{"key"})
        addr=request.client.host if request.client else "local"
        recent=[v for v in failures[addr] if time.time()-v<300]
        failures[addr]=recent
        if len(recent)>=10: raise Problem("FORBIDDEN","登录尝试过多，请稍后重试",403)
        if not isinstance(payload["key"],str) or not secrets.compare_digest(payload["key"],admin_key):
            recent.append(time.time())
            raise Problem("UNAUTHENTICATED","管理密钥无效",401)
        failures[addr]=[]
        sid=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32)
        sessions[sid]={"csrf":csrf,"expires":time.time()+8*3600}
        response=JSONResponse({"csrf_token":csrf,"expires_in":8*3600})
        response.set_cookie("pm_session",sid,httponly=True,samesite="strict",max_age=8*3600,path="/")
        return response

    @app.delete("/api/session")
    def logout(request:Request):
        sessions.pop(request.cookies.get("pm_session",""),None)
        response=JSONResponse({"signed_out":True}); response.delete_cookie("pm_session",path="/")
        return response

    @app.get("/api/session")
    def current_session(request:Request):
        return {"csrf_token":request.state.session["csrf"],"expires_in":max(0,int(request.state.session["expires"]-time.time()))}

    @app.get("/api/status")
    def status():
        with service.db.read() as conn:
            discovered=conn.execute("SELECT COUNT(*) FROM workspace_aliases WHERE resolution!='personal'").fetchone()[0]
        discovery=None
        try: discovery=json.loads(discovery_status.read_text("utf8"))
        except (OSError,ValueError): pass
        history=[]
        try:
            for line in discovery_history.read_text("utf8").splitlines()[-20:]:
                try: history.append(json.loads(line))
                except ValueError: continue
        except OSError: pass
        return {"status":"ok","schema_version":SCHEMA_VERSION,"data_dir":str(data_dir),
            "mcp_url":f"http://127.0.0.1:{port}/mcp/","automation":"library-automatic-v2",
            "access_mode":"library","discovered_projects":discovered,"host_discovery":discovery,
            "host_discovery_history":history,
            "mcp_access":"read-only","automation_access":"read-write","excluded_roots_supported":True}

    @app.get("/api/projects")
    def projects(): return service.list_projects(admin)

    @app.post("/api/projects")
    async def project_create(request:Request):
        p=fields(await body(request),{"name","description"},{"name"})
        return service.create_project(admin,p["name"],p.get("description",""),key(request))

    @app.patch("/api/projects/{project_id}")
    async def project_update(project_id:str,request:Request):
        p=fields(await body(request),{"name","description","status","expected_version","reason"},{"expected_version","reason"})
        return service.update_project(admin,project_id,p,key(request))

    @app.get("/api/memories")
    def memories(project_id:str|None=None,status:str="Active",type:str|None=None,limit:int=50,offset:int=0):
        return service.list_memories(admin,project_id,status,type,limit,offset)

    @app.post("/api/memories")
    async def memory_create(request:Request):
        p=fields(await body(request),{"project_id","content","type","confidence","source_content","source_title","source_id","source_span","decision_key","visibility","activate"},{"project_id","content","type","confidence"})
        return service.create_memory(admin,**p,idempotency_key=key(request))

    @app.post("/api/memories/search")
    async def memory_search(request:Request):
        p=fields(await body(request),{"query","project_id","project_ids","include_global","limit","history_status","mode"},{"query"})
        mode=p.pop("mode","keyword")
        if mode=="keyword": return service.search(admin,**p)
        if mode=="hybrid" and not p.get("history_status"): return semantic.hybrid_search(admin,**p)
        raise Problem("VALIDATION_ERROR","检索模式无效",422)

    @app.post("/api/vectors/rebuild")
    def vector_rebuild():
        return semantic.start_rebuild(admin)

    @app.get("/api/vectors/jobs/{job_id}")
    def vector_job(job_id:str):
        return semantic.get_job(admin,job_id)

    @app.get("/api/memories/{memory_id}")
    def memory_detail(memory_id:str,include_history:bool=False,source_offset:int=0):
        return service.get_memory(admin,memory_id,include_history,source_offset)

    @app.get("/api/memories/{memory_id}/revisions")
    def memory_revisions(memory_id:str):
        return service.get_memory(admin,memory_id,True)["revisions"]

    @app.patch("/api/memories/{memory_id}")
    async def memory_edit(memory_id:str,request:Request):
        p=fields(await body(request),{"expected_version","changes","reason"},{"expected_version","changes","reason"})
        return service.edit_memory(admin,memory_id,p["expected_version"],p["changes"],p["reason"],key(request))

    @app.post("/api/memories/{memory_id}/approve")
    async def memory_approve(memory_id:str,request:Request):
        p=fields(await body(request),{"expected_version"},{"expected_version"})
        return service.approve_candidate(admin,memory_id,p["expected_version"],key(request))

    @app.post("/api/memories/{memory_id}/supersede")
    async def memory_supersede(memory_id:str,request:Request):
        p=fields(await body(request),{"expected_version","new_id","expected_new_version","reason"},{"expected_version","new_id","expected_new_version","reason"})
        return service.supersede(admin,memory_id,p["expected_version"],p["new_id"],p["expected_new_version"],p["reason"],key(request))

    for action in ("archive","reopen","delete","restore"):
        def route(memory_id:str,request:Request,_action=action):
            # Created below through an async factory so request bodies are read normally.
            raise RuntimeError("unreachable")
        async def make_route(memory_id:str,request:Request,_action=action):
            p=fields(await body(request),{"expected_version","reason"},{"expected_version","reason"})
            return service.transition(admin,memory_id,p["expected_version"],_action,p["reason"],key(request))
        app.add_api_route(f"/api/memories/{{memory_id}}/{action}",make_route,methods=["POST"],name=f"memory_{action}")

    @app.get("/api/sources/{source_id}")
    def source_detail(source_id:str,offset:int=0): return service.get_source(admin,source_id,offset)

    @app.post("/api/sources")
    async def source_create(request:Request):
        p=fields(await body(request),{"project_id","content","title","visibility"},{"project_id","content","title"})
        return service.create_source(admin,p["project_id"],p["content"],p["title"],p.get("visibility","local_only"),key(request))

    @app.get("/api/sources")
    def source_list(project_id:str,limit:int=100):
        return service.list_sources(admin,project_id,limit)

    @app.get("/api/sources/{source_id}/full")
    def source_full(source_id:str):
        with service.db.read() as conn:
            row=conn.execute("SELECT * FROM sources WHERE source_id=?",(source_id,)).fetchone()
            if not row: raise Problem("NOT_FOUND","来源不可用",404)
            source=service._source(conn,source_id,row["project_id"],admin)
            return {"source_id":source_id,"project_id":source["project_id"],"title":source["title"],"content":source["content"],"visibility":source["visibility"],"version":source["version"]}

    @app.post("/api/imports/preview")
    async def import_preview(request:Request):
        p=fields(await body(request),{"filename","base64","encoding"},{"filename","base64","encoding"})
        return decode_upload(p["filename"],p["base64"],p["encoding"])

    @app.post("/api/imports")
    async def import_file(request:Request):
        p=fields(await body(request),{"project_id","filename","base64","encoding","expected_text_sha256","confirmed"},{"project_id","filename","base64","encoding","expected_text_sha256","confirmed"})
        if p["confirmed"] is not True: raise Problem("CONSENT_REQUIRED","请先核对解码预览",422)
        return service.import_file_source(admin,p["project_id"],p,p["expected_text_sha256"],key(request))

    @app.post("/api/extractions/preview")
    async def extraction_preview(request:Request):
        p=fields(await body(request),{"source_id","start","end"},{"source_id","start","end"})
        return extraction.preview(admin,p["source_id"],p["start"],p["end"])

    @app.post("/api/extractions")
    async def extraction_start(request:Request):
        p=fields(await body(request),{"source_id","start","end","expected_text_sha256","confirmed"},{"source_id","start","end","expected_text_sha256","confirmed"})
        return extraction.start(admin,p["source_id"],p["start"],p["end"],p["expected_text_sha256"],p["confirmed"],key(request))

    @app.get("/api/extractions/{job_id}")
    def extraction_status(job_id:str):
        return extraction.get(admin,job_id)

    @app.patch("/api/sources/{source_id}/visibility")
    async def source_visibility(source_id:str,request:Request):
        p=fields(await body(request),{"visibility","expected_version"},{"visibility","expected_version"})
        return service.set_source_visibility(admin,source_id,p["visibility"],p["expected_version"],key(request))

    @app.get("/api/change-requests")
    def changes(status:str="pending"): return service.list_changes(admin,status)

    for decision in ("approve","reject"):
        async def review(request_id:str,request:Request,_decision=decision):
            p=fields(await body(request),{"expected_version"},{"expected_version"})
            return service.review_change(admin,request_id,p["expected_version"],_decision=="approve",key(request))
        app.add_api_route(f"/api/change-requests/{{request_id}}/{decision}",review,methods=["POST"],name=f"change_{decision}")

    @app.get("/api/clients")
    def clients(): return service.list_clients(admin)

    @app.post("/api/clients")
    async def client_create(request:Request):
        p=fields(await body(request),{"name","project_ids","can_write"},{"name","project_ids","can_write"})
        return service.create_client(admin,p["name"],p["project_ids"],p["can_write"])

    @app.post("/api/clients/{client_id}/revoke")
    def client_revoke(client_id:str): return service.revoke_client(admin,client_id)

    @app.post("/api/backup")
    def backup(): return service.backup(admin,data_dir/"backups")

    @app.get("/api/backups")
    def backups():
        return [{"backup_id":p.stem,"size":p.stat().st_size} for p in sorted((data_dir/"backups").glob("*.zip"))] if (data_dir/"backups").exists() else []

    @app.get("/api/backups/{backup_id}/download")
    def backup_download(backup_id:str):
        try: uuid.UUID(backup_id)
        except ValueError: raise Problem("NOT_FOUND","备份不存在",404)
        path=data_dir/"backups"/(backup_id+".zip")
        if not path.is_file(): raise Problem("NOT_FOUND","备份不存在",404)
        return FileResponse(path,filename=path.name)

    @app.post("/api/exports")
    def export(): return service.export_json(admin,data_dir/"exports")

    @app.get("/api/exports/{export_id}/download")
    def export_download(export_id:str):
        try: uuid.UUID(export_id)
        except ValueError: raise Problem("NOT_FOUND","导出不存在",404)
        path=data_dir/"exports"/(export_id+".json")
        if not path.is_file(): raise Problem("NOT_FOUND","导出不存在",404)
        return FileResponse(path,filename=path.name)

    @app.post("/automation/v1/context")
    def automatic_context(payload:ContextRequest,request:Request):
        return automation.context(request.state.automatic_client,payload)

    @app.post("/automation/v1/turns",status_code=202)
    def automatic_turn(payload:TurnRequest,request:Request):
        return automation.append(request.state.automatic_client,payload)

    @app.post("/automation/v2/context")
    def automatic_library_context(payload:LibraryContextRequest,request:Request):
        return automation.library_context(request.state.automatic_client,payload)

    @app.post("/automation/v2/turns",status_code=202)
    def automatic_library_turn(payload:LibraryTurnRequest,request:Request):
        return automation.library_append(request.state.automatic_client,payload)


    @app.get("/automation/v2/task-checkpoints")
    def task_checkpoint_list(request:Request,project_id:str,source_app:str|None=None,limit:int=5):
        return task_checkpoints.list_checkpoints(service,request.state.automatic_client,project_id,source_app,limit)

    @app.get("/automation/v2/task-checkpoints/{task_id}")
    def task_checkpoint_get(task_id:str,request:Request,archive_id:str|None=None,offset:int=0):
        return task_checkpoints.get_checkpoint(service,request.state.automatic_client,task_id,archive_id,offset)

    @app.get("/automation/v2/task-contracts/{task_id}")
    def task_contract_get(task_id:str,request:Request,session_id:str|None=None,cwd:str|None=None):
        from . import task_contracts
        return {"contract":task_contracts.for_client(service,request.state.automatic_client,task_id,session_id,cwd)}

    @app.post("/automation/v2/task-contracts/{task_id}/pre-tool")
    async def task_contract_pre_tool(task_id:str,request:Request):
        from . import task_contracts
        p=fields(await body(request),{"session_id","turn_id","tool_use_id","tool_name","tool_input","cwd","prompt","input_digests","attempt"},{"session_id","tool_use_id","tool_name"})
        return task_contracts.pre_tool(service,request.state.automatic_client,task_id,p)

    @app.post("/automation/v2/task-contracts/{task_id}/post-tool")
    async def task_contract_post_tool(task_id:str,request:Request):
        from . import task_contracts
        p=fields(await body(request),{"session_id","turn_id","tool_use_id","tool_name","tool_input","tool_response","exit_code","cwd","artifacts","attempt","contract_version"},{"session_id","tool_use_id","tool_name"})
        return task_contracts.post_tool(service,request.state.automatic_client,task_id,p)

    @app.get("/automation/v2/task-preparation")
    def task_preparation_get(request:Request,session_id:str,cwd:str):
        from . import task_preparations
        return task_preparations.for_session(service,request.state.automatic_client,session_id,cwd) or {"state":"unavailable"}

    @app.post("/api/automation/task-preparations/preview")
    async def task_preparation_preview(request:Request):
        from . import task_preparations
        p=fields(await body(request),{"definition"},{"definition"})
        return task_preparations.preview(service,admin,p["definition"])

    @app.post("/api/automation/task-preparations")
    async def task_preparation_commit(request:Request):
        from . import task_preparations
        p=fields(await body(request),{"definition","expected","reason"},{"definition","expected","reason"})
        return task_preparations.commit(service,admin,p["definition"],p["expected"],p["reason"],request.headers.get("Idempotency-Key"))

    @app.post("/api/automation/task-preparations/{task_id}/cancel")
    async def task_preparation_cancel(task_id:str,request:Request):
        from . import task_preparations
        p=fields(await body(request),{"expected_revision","reason"},{"expected_revision","reason"})
        return task_preparations.cancel(service,admin,task_id,p["expected_revision"],p["reason"])

    @app.get("/api/automation/task-contracts/{task_id}")
    def task_contract_history(task_id:str):
        from . import task_contracts
        return task_contracts.history(service,admin,task_id)

    @app.put("/api/automation/task-contracts/{task_id}")
    async def task_contract_set(task_id:str,request:Request):
        from . import task_contracts
        p=fields(await body(request),{"data","expected_version","reason"},{"data","expected_version","reason"})
        return task_contracts.set_contract(service,admin,task_id,p["data"],p["expected_version"],p["reason"])

    @app.post("/api/automation/task-contracts/{task_id}/restore")
    async def task_contract_restore(task_id:str,request:Request):
        from . import task_contracts
        p=fields(await body(request),{"version","expected_version"},{"version","expected_version"})
        return task_contracts.restore(service,admin,task_id,p["version"],p["expected_version"])

    @app.post("/api/automation/task-contracts/{task_id}/disable")
    async def task_contract_disable(task_id:str,request:Request):
        from . import task_contracts
        p=fields(await body(request),{"expected_version","reason"},{"expected_version","reason"})
        return task_contracts.disable(service,admin,task_id,p["expected_version"],p["reason"])

    @app.post("/api/automation/task-checkpoints/{task_id}/annotations")
    async def task_annotation_add(task_id:str,request:Request):
        p=fields(await body(request),{"kind","subject_archive_id","data"},{"kind","subject_archive_id","data"})
        return task_checkpoints.annotate(service,admin,task_id,p["kind"],p["subject_archive_id"],p["data"])

    @app.put("/api/automation/task-checkpoints/{task_id}/annotations/{annotation_id}")
    async def task_annotation_state(task_id:str,annotation_id:str,request:Request):
        p=fields(await body(request),{"active"},{"active"})
        return task_checkpoints.set_annotation_active(service,admin,task_id,annotation_id,p["active"])

    @app.get("/api/automation/task-checkpoints/grants")
    def task_checkpoint_grants():
        with service.db.read() as conn:
            return {"items":[dict(r) for r in conn.execute("SELECT client_id,project_id,allowed,updated_at FROM task_checkpoint_grants ORDER BY project_id,client_id")]}

    @app.get("/admin/relay",response_class=HTMLResponse)
    def admin_relay(request:Request,project_id:str=""):
        session=sessions.get(request.cookies.get("pm_session",""))
        if not session or session["expires"]<time.time():
            raise Problem("UNAUTHENTICATED","管理会话已失效",401)
        candidates=[p for p in projects() if p["project_id"] not in {"global","personal-sessions"}]
        project=next((p for p in candidates if p["project_id"]==project_id),None) if project_id else (candidates[0] if candidates else None)
        if not project:
            raise Problem("NOT_FOUND","任务接力项目不存在",404)
        project_id=project["project_id"]
        grants={g["client_id"]:g for g in task_checkpoint_grants()["items"] if g["project_id"]==project_id}
        rows=[]
        for client in automatic_status()["clients"]:
            if client["access_mode"]!="library" or client["app"] not in task_checkpoints.APPS:
                continue
            cid=escape(client["client_id"],quote=True)
            grant=grants.get(client["client_id"])
            allowed=bool(grant and grant["allowed"])
            available=bool(client["enabled"] and not client["revoked"])
            client_state="凭据已撤销" if client["revoked"] else "已启用" if client["enabled"] else "已停用"
            grant_state="已授权" if allowed else "已撤销" if grant else "未授权"
            authorize_disabled=" disabled" if allowed or not available else ""
            revoke_disabled=" disabled" if not allowed or not available else ""
            rows.append(
                f'<tr data-client-id="{cid}"><td><strong>{escape(client["app"])}</strong>'
                f'<br><code>{cid}</code><br><small>{client_state}</small></td>'
                f'<td>{grant_state}</td><td><button type="button" data-allowed="true"{authorize_disabled}>授权</button> '
                f'<button type="button" data-allowed="false"{revoke_disabled}>撤销</button></td></tr>'
            )
        if not rows:
            rows.append('<tr><td colspan="3">没有可管理的任务接力客户端。</td></tr>')
        tasks=[]
        with service.db.read() as conn:
            for task in conn.execute(
                    "SELECT c.task_id,c.origin_root,c.created_at,"
                    "(SELECT MAX(version) FROM task_contracts t WHERE t.task_id=c.task_id) AS version "
                    "FROM task_checkpoints c WHERE c.project_id=? ORDER BY c.created_at DESC LIMIT 20",
                    (project_id,)):
                task=dict(task)
                data=None
                if task["version"]:
                    latest=conn.execute("SELECT data_json FROM task_contracts WHERE task_id=? ORDER BY version DESC LIMIT 1",
                                        (task["task_id"],)).fetchone()
                    data=json.loads(latest["data_json"]) if latest else None
                tasks.append((task,data))
        blocks=[]
        for task,data in tasks:
            tid=escape(task["task_id"],quote=True)
            version=task["version"] or 0
            if data and (data.get("path_roles") or data.get("checks") or len(data.get("steps") or [])!=1
                         or any((s.get("artifact") or s.get("marker") or s.get("basis")) for s in data.get("steps") or [])):
                blocks.append(
                    f'<details class="task"><summary><strong>{escape(task["task_id"])}</strong> · 合约 v{version}'
                    f'<br><small>{escape(task["origin_root"])}（多步骤/检查/路径角色，按原样编辑）</small></summary>'
                    f'<form data-task="{tid}" data-version="{version}" data-mode="json">'
                    f'<label>完整合约（JSON）<textarea name="json" rows="16">'
                    f'{escape(json.dumps(data,ensure_ascii=False,indent=2))}</textarea></label>'
                    '<p class="form-error" hidden></p><button type="submit">保存合约</button></form></details>'
                )
                continue
            if data:
                steps=data.get("steps") or [{}]
                phase_id=data.get("phase_id","")
                label=steps[0].get("label","")
                artifacts="\n".join(data.get("allowed_artifacts") or [])
                forbidden="\n".join(data.get("forbidden") or [])
                state=f'合约 v{version}'
            else:
                phase_id="phase-"+task["task_id"].replace("task_","")[:12]
                label=""
                artifacts=""
                forbidden=""
                state="<em>尚无合约，这个任务会被拦住</em>"
            blocks.append(
                f'<details class="task"><summary><strong>{escape(task["task_id"])}</strong> · {state}'
                f'<br><small>{escape(task["origin_root"])}</small></summary>'
                f'<form data-task="{tid}" data-version="{version}" data-mode="simple">'
                f'<label>阶段编号 <input name="phase_id" value="{escape(phase_id,quote=True)}" size="30"></label>'
                f'<label>这个任务要做什么 <input name="label" value="{escape(label,quote=True)}" size="80"></label>'
                f'<label>允许改的文件（每行一个，相对项目根）<textarea name="artifacts" rows="4">'
                f'{escape(artifacts)}</textarea></label>'
                f'<label>禁止事项（每行一条）<textarea name="forbidden" rows="3">'
                f'{escape(forbidden)}</textarea></label>'
                '<p class="form-error" hidden></p><button type="submit">保存合约</button></form></details>'
            )
        task_section=('<h2>任务合约</h2><p>没有合约的任务会被拦截，读得到文件但改不了。'
                      '这里填「要做什么」和「允许改哪些文件」，保存后立即生效。</p>'
                      + ("".join(blocks) if blocks else '<p>这个项目还没有任务检查点。</p>'))
        page=(
            """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>任务接力授权管理</title>
"""
            f'<meta name="relay-project-id" content="{escape(project_id,quote=True)}">\n'
            f'<meta name="csrf-token" content="{escape(session["csrf"],quote=True)}">\n'
            """<style>
body { font-family: system-ui, sans-serif; margin: 32px auto; padding: 0 20px; max-width: 960px; color: #202534; }
table { width: 100%; border-collapse: collapse; margin-top: 24px; }
th, td { padding: 14px; border-bottom: 1px solid #dfe3eb; text-align: left; }
th { background: #f4f6fa; }
code { overflow-wrap: anywhere; }
small { color: #596273; }
button { padding: 7px 14px; cursor: pointer; }
button:disabled { cursor: default; opacity: .5; }
#error { color: #b42318; }
.form-error { color: #b42318; margin: 10px 0 0; }
details.task { margin: 18px 0; padding: 14px; border: 1px solid #dfe3eb; border-radius: 8px; }
details.task summary { cursor: pointer; }
label { display: block; margin: 12px 0; }
input, textarea { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 14px; padding: 7px; }
</style>
</head>
<body>
<h1>任务接力授权管理</h1>
"""
            f'<p>当前项目：<strong>{escape(project["name"])}</strong><br><code>{escape(project_id)}</code></p>\n'
            """<p>授权与撤销仅作用于此项目的任务接力权限。</p>
<p id="error" role="alert" hidden></p>
<table>
<thead><tr><th scope="col">客户端</th><th scope="col">接力授权</th><th scope="col">操作</th></tr></thead>
<tbody>
"""
            +"\n".join(rows)+
            """
</tbody>
</table>
"""+task_section+r"""
<noscript><p>请启用 JavaScript 后使用授权与撤销按钮。</p></noscript>
<script>
const projectId = document.querySelector('meta[name="relay-project-id"]').content;
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const errorBox = document.getElementById('error');
let pending = false;
document.querySelector('tbody').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-allowed]');
    if (!button || button.disabled || pending) return;
    const clientId = button.closest('tr').dataset.clientId;
    const enabledButtons = Array.from(document.querySelectorAll('button[data-allowed]:not([disabled])'));
    pending = true;
    enabledButtons.forEach((item) => { item.disabled = true; });
    errorBox.hidden = true;
    try {
        const response = await fetch('/api/automation/task-checkpoints/grants', {
            method: 'PUT',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
            body: JSON.stringify({ client_id: clientId, project_id: projectId, allowed: button.dataset.allowed === 'true' })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.message || `操作失败（${response.status}）`);
        window.location.reload();
    } catch (error) {
        errorBox.textContent = error.message || '请求失败，请稍后重试。';
        errorBox.hidden = false;
    } finally {
        enabledButtons.forEach((item) => { item.disabled = false; });
        pending = false;
    }
});
document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-task]');
    if (!form || pending) return;
    event.preventDefault();
    errorBox.hidden = true;
    const formError = form.querySelector('.form-error');
    const showError = (message) => {
        if (formError) { formError.textContent = message; formError.hidden = false; }
        errorBox.textContent = message; errorBox.hidden = false;
    };
    if (formError) formError.hidden = true;
    const taskId = form.dataset.task;
    const version = Number(form.dataset.version) || null;
    let data;
    if (form.dataset.mode === 'json') {
        try { data = JSON.parse(form.elements.json.value); }
        catch (error) { showError('JSON 格式有误：' + error.message); return; }
    } else {
        const lines = (value) => value.split('\n').map((item) => item.trim()).filter(Boolean);
        const label = form.elements.label.value.trim();
        const phaseId = (form.elements.phase_id.value.trim() || ('phase-' + taskId.replace(/^task_/, '').slice(0, 12)))
            .replace(/[^A-Za-z0-9_.-]/g, '-');
        data = {
            phase_id: phaseId,
            phase_label: label,
            steps: [{ step_id: 'step-1', label: label, state: 'authorized', artifact: null, marker: null, basis: [] }],
            allowed_artifacts: lines(form.elements.artifacts.value).map((item) => {
                const path = item.split(String.fromCharCode(92)).join('/');
                return path.indexOf('./') === 0 ? path.slice(2) : path;
            }),
            progress_carrier: 'pm_checkpoint',
            forbidden: lines(form.elements.forbidden.value),
            checks: [], enforce: true, notice: '', execution_mode: 'scoped_write'
        };
    }
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    pending = true;
    try {
        const response = await fetch('/api/automation/task-contracts/' + encodeURIComponent(taskId), {
            method: 'PUT',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
            body: JSON.stringify({ data, expected_version: version, reason: '任务接力管理页面保存' })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.message || result.detail || ('保存失败（' + response.status + '）'));
        window.location.reload();
    } catch (error) {
        showError(error.message || '保存失败，请稍后重试。');
    } finally {
        pending = false;
        if (button) button.disabled = false;
    }
});
</script>
</body>
</html>
"""
        )
        return HTMLResponse(page,headers={"Cache-Control":"no-store"})

    @app.get("/api/automation/workspace-directory-mappings")
    def workspace_directory_mappings():
        from .workspaces import list_directory_mappings
        return list_directory_mappings(service,admin)

    @app.post("/api/automation/workspace-directory-mappings")
    async def workspace_directory_mapping_add(request:Request):
        from .workspaces import add_directory_mapping
        p=fields(await body(request),{"directory","workspace_root","project_id"},{"directory","workspace_root","project_id"})
        return add_directory_mapping(service,admin,p["directory"],p["workspace_root"],p["project_id"])

    @app.put("/api/automation/workspace-directory-mappings/{mapping_id}")
    async def workspace_directory_mapping_state(mapping_id:str,request:Request):
        from .workspaces import set_directory_mapping_active
        p=fields(await body(request),{"active","expected_version"},{"active","expected_version"})
        return set_directory_mapping_active(service,admin,mapping_id,p["active"],p["expected_version"])

    @app.put("/api/automation/task-checkpoints/grants")
    async def task_checkpoint_grant(request:Request):
        p=fields(await body(request),{"client_id","project_id","allowed"},{"client_id","project_id","allowed"})
        return task_checkpoints.grant(service,admin,p["client_id"],p["project_id"],p["allowed"])

    @app.get("/automation/v2/conversations/search")
    def conversation_search(request:Request, query:str, limit:int=5):
        return conversation_archive.search(service,request.state.automatic_client,query,limit)

    @app.get("/automation/v2/conversations/{archive_id}")
    def conversation_get(archive_id:str,request:Request,offset:int=0):
        return conversation_archive.get(service,request.state.automatic_client,archive_id,offset)

    @app.get("/automation/v1/jobs/{job_id}")
    def automatic_job(job_id:str,request:Request):
        return automation.job(request.state.automatic_client,job_id)

    @app.get("/api/automation")
    def automatic_status(): return automation.status()

    @app.get("/api/automation/decisions")
    def automatic_decisions(limit:int=100,project_id:str|None=None):return automatic_review.recent(service,admin,limit,project_id)

    @app.get("/api/automation/retrieval-diagnostics")
    def automatic_retrieval_diagnostics(limit:int=50):
        return automation.retrieval_diagnostics(admin,limit)

    @app.post("/api/automation/memories/{memory_id}/undo")
    async def undo_automatic_memory(memory_id:str,request:Request):
        p=fields(await body(request),{"expected_version"},{"expected_version"})
        return automatic_review.undo(service,admin,memory_id,p["expected_version"])

    @app.post("/api/exports/markdown")
    async def readable_export(request:Request):
        p=fields(await body(request),{"project_id","include_private"},set())
        from fastapi.responses import Response
        text=automatic_review.markdown(service,admin,p.get("project_id"),p.get("include_private",False))
        return Response(text,media_type="text/markdown; charset=utf-8",headers={"Content-Disposition":'attachment; filename="personal-memory.md"'})

    @app.post("/api/automation/clients")
    async def automatic_client_create(request:Request):
        p=fields(await body(request),{"app","project_id","roots"},{"app","project_id","roots"})
        return automation.create_client(admin,p["app"],p["project_id"],p["roots"])

    @app.post("/api/automation/library-clients")
    async def automatic_library_client_create(request:Request):
        p=fields(await body(request),{"app","excluded_roots","purpose"})
        if p.get("purpose")=="mcp" and request.headers.get("x-personal-memory-installer")!="local-installer":
            raise Problem("FORBIDDEN","只读检索凭据只能由本机安装器创建",403)
        return automation.create_library_client(admin,p["app"],p.get("excluded_roots",[]),p.get("purpose","automation"))

    @app.patch("/api/automation/clients/{client_id}")
    async def automatic_client_state(client_id:str,request:Request):
        p=fields(await body(request),{"enabled","revoke"},{"enabled"})
        if type(p["enabled"]) is not bool or type(p.get("revoke",False)) is not bool:
            raise Problem("VALIDATION_ERROR","状态格式无效",422)
        return automation.set_client(admin,client_id,p["enabled"],p.get("revoke",False))

    @app.post("/api/automation/jobs/{job_id}/retry")
    def automatic_retry(job_id:str): return automation.retry(admin,job_id)

    @app.put("/api/automation/clients/{client_id}/exclusions")
    async def automatic_client_exclusions(client_id:str,request:Request):
        p=fields(await body(request),{"roots"},{"roots"})
        return automation.set_exclusions(admin,client_id,p["roots"])

    @app.put("/api/automation/clients/{client_id}/archive-read")
    async def automatic_archive_grant(client_id:str,request:Request):
        p=fields(await body(request),{"allowed"},{"allowed"})
        return conversation_archive.grant(service,admin,client_id,p["allowed"])

    @app.get("/api/automation/conversations/search")
    def admin_conversation_search(client_id:str,query:str,limit:int=5):
        return conversation_archive.search(service,{"client_id":client_id},query,limit)

    @app.get("/api/automation/conversations/{archive_id}")
    def admin_conversation_get(archive_id:str,client_id:str,offset:int=0):
        return conversation_archive.get(service,{"client_id":client_id},archive_id,offset)

    @app.post("/api/automation/conversations/delete")
    async def automatic_archive_delete(request:Request):
        p=fields(await body(request),{"client_id","session_id","revoke_memories"},{"client_id","session_id","revoke_memories"})
        return conversation_archive.delete_session(service,admin,p["client_id"],p["session_id"],p["revoke_memories"])

    @app.get("/api/automation/conversation-source-corrections")
    def source_corrections():
        return conversation_provenance.list_corrections(service, admin)

    @app.post("/api/automation/conversation-source-corrections")
    async def source_correction_add(request: Request):
        required={"event_id","expected_content_hash","reason","evidence"}
        p=fields(await body(request),required,required)
        return conversation_provenance.correct(service,admin,p["event_id"],p["expected_content_hash"],p["reason"],p["evidence"])

    @app.put("/api/automation/conversation-source-corrections/{correction_id}")
    async def source_correction_state(correction_id: str, request: Request):
        p=fields(await body(request),{"active"},{"active"})
        return conversation_provenance.set_active(service,admin,correction_id,p["active"])

    @app.post("/api/automation/jobs/{job_id}/revoke")
    def automatic_revoke(job_id:str): return automation.revoke_job(admin,job_id)

    app.mount("/mcp",mcp_app,name="mcp")
    frontend=frontend_dir or Path(__file__).parent.parent/"frontend"/"dist"
    if frontend.exists():
        app.mount("/",StaticFiles(directory=frontend,html=True),name="frontend")
    else:
        @app.get("/")
        def missing_frontend(): return JSONResponse({"code":"FRONTEND_NOT_BUILT","message":"请先构建 frontend"},status_code=503)
    return app
