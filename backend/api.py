from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
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
    def task_contract_get(task_id:str,request:Request):
        from . import task_contracts
        return {"contract":task_contracts.for_client(service,request.state.automatic_client,task_id)}

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
