"""Prepare Doubao Work stdio MCP without printing or embedding credentials."""
from __future__ import annotations
import argparse,asyncio,http.cookiejar,json,os,urllib.request,uuid,winreg
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
DATA=Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")))
BASE=os.environ.get("PERSONAL_MEMORY_BASE", "http://127.0.0.1:8765")
READ_ENV="PERSONAL_MEMORY_MCP_DOUBAO_TOKEN"
WRITE_ENV="PERSONAL_MEMORY_DOUBAO_CANDIDATE_TOKEN"

def env_get(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,"Environment") as k:return winreg.QueryValueEx(k,name)[0]
    except OSError:return None
def env_set(name,value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,"Environment") as k:
        if value is None:
            try:winreg.DeleteValue(k,name)
            except FileNotFoundError:pass
        else:winreg.SetValueEx(k,name,0,winreg.REG_SZ,value)
def request(opener,path,body=None,csrf=None,method=None):
    headers={"Content-Type":"application/json","Origin":BASE}
    if csrf:headers.update({"x-csrf-token":csrf,"Idempotency-Key":str(uuid.uuid4()),"x-personal-memory-installer":"local-installer"})
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    with opener.open(req,timeout=30) as response:return json.load(response)

async def verify(config):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters,stdio_client
    environment=dict(os.environ)
    for name in (READ_ENV,WRITE_ENV):environment.pop(name,None)
    params=StdioServerParameters(command=str(ROOT/".venv/Scripts/python.exe"),args=[str(ROOT/"doubao_work_bridge.py"),"--config",str(config)],env=environment)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            names=sorted(t.name for t in (await session.list_tools()).tools)
            assert names==["get_memory","save_memory_candidate","search_memories"]
            result=await session.call_tool("search_memories",{"query":"Personal Memory connection check","limit":2})
            assert not result.is_error
            payload=json.loads(result.content[0].text)
            return {"tools":names,"search_ok":True,"matches":len(payload.get("items",[]))}

def install(project_id):
    folder=DATA/"connectors/doubao";config=folder/"config.json"
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=request(opener,"/api/session",{"key":(DATA/"admin-key.txt").read_text().strip()})["csrf_token"]
    created=[];saved=[];written=False
    try:
        projects=request(opener,"/api/projects")
        project=next(p for p in projects if p["project_id"]==project_id and p["status"]=="active")
        if config.exists():
            current=json.loads(config.read_text(encoding="utf-8-sig"))
            if current["write_project_id"]!=project_id or not all(env_get(n) for n in (READ_ENV,WRITE_ENV)):
                raise RuntimeError("Existing Doubao configuration differs or credentials are incomplete")
            return {"idempotent":True,"configuration":str(config),**asyncio.run(verify(config))}
        if any(env_get(n) for n in (READ_ENV,WRITE_ENV)):raise RuntimeError("Unowned Doubao credentials exist; refusing replacement")
        backup=request(opener,"/api/backup",{},csrf)
        reader=request(opener,"/api/automation/library-clients",{"app":"doubao","excluded_roots":[],"purpose":"mcp"},csrf);created.append(("reader",reader["client_id"]))
        writer=request(opener,"/api/clients",{"name":"Doubao Work project candidate writer","project_ids":[project_id],"can_write":True},csrf);created.append(("writer",writer["client_id"]))
        for name,value in ((READ_ENV,reader.pop("token")),(WRITE_ENV,writer.pop("token"))):
            env_set(name,value);saved.append(name)
        folder.mkdir(parents=True,exist_ok=True)
        settings={"host":"doubao-work","read_client_id":reader["client_id"],"write_client_id":writer["client_id"],
            "write_project_id":project_id,"write_project_name":project["name"],"read_token_env":READ_ENV,"write_token_env":WRITE_ENV,
            "automatic_capture":False,"write_policy":"candidate-local-review","base_url":BASE}
        temp=config.with_suffix(".tmp");temp.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding="utf8");os.replace(temp,config);written=True
        check=asyncio.run(verify(config))
        result={"configuration_saved":True,"configuration":str(config),"read_client_id":reader["client_id"],"write_client_id":writer["client_id"],
            "write_project_id":project_id,"read_mode":"authorized-library","write_mode":"project-candidate-local-review",
            "backup_id":backup["backup_id"],**check,"desktop_test":"NOT_RUN"}
        (folder/"installation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
        return result
    except Exception:
        if written:config.unlink(missing_ok=True)
        for name in saved:env_set(name,None)
        for kind,cid in reversed(created):
            try:
                if kind=="reader":request(opener,"/api/automation/clients/"+cid,{"enabled":False,"revoke":True},csrf,"PATCH")
                else:request(opener,"/api/clients/"+cid+"/revoke",{},csrf)
            except Exception:pass
        raise
    finally:request(opener,"/api/session",None,csrf,"DELETE")

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("command",choices=("install","verify"));parser.add_argument("--project-id")
    args=parser.parse_args()
    if args.command=="install":
        if not args.project_id:parser.error("--project-id required")
        print(json.dumps(install(args.project_id),ensure_ascii=False))
    else:print(json.dumps(asyncio.run(verify(DATA/"connectors/doubao/config.json")),ensure_ascii=False))
