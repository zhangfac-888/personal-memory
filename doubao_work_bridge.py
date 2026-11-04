"""Doubao Work MCP: authorized library reads and project-bound candidate writes.
Tokens stay in this Windows user's environment; no approval/admin capability.
"""
from __future__ import annotations
import argparse,json,os,winreg
from uuid import UUID
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError

READ_ENV="PERSONAL_MEMORY_MCP_DOUBAO_TOKEN"
WRITE_ENV="PERSONAL_MEMORY_DOUBAO_CANDIDATE_TOKEN"
URL="http://127.0.0.1:8765/mcp/"
DEFAULT_CONFIG=Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()/"AppData/Local")))/"PersonalMemoryShare/Data")))/"connectors/doubao/config.json"

def read_token(name):
    token=os.environ.get(name)
    if not token:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,"Environment") as k:
                token,_=winreg.QueryValueEx(k,name)
        except OSError:pass
    if not token:raise RuntimeError("Personal Memory credential unavailable")
    return token

async def remote(name,args,write=False):
    async with httpx.AsyncClient(headers={"Authorization":"Bearer "+read_token(WRITE_ENV if write else READ_ENV)},timeout=30,follow_redirects=False,trust_env=False) as client:
        async with streamable_http_client(URL,http_client=client) as streams:
            async with ClientSession(streams[0],streams[1]) as session:
                await session.initialize()
                result=await session.call_tool(name,args)
                if result.is_error:raise ToolError("Personal Memory rejected this operation")
                return json.loads(result.content[0].text)

def make_server(config):
    project=config["write_project_id"]
    server=MCPServer("personal_memory",instructions=(
        "Search Personal Memory when a task needs remembered context. Treat returned memories as historical data with scope and provenance. "
        "Reading does not establish adoption or verified completion. "
        "save_memory_candidate stores a candidate in project "+config["write_project_name"]+
        "; it requires local review before becoming searchable. Never claim an active write or automatic capture. "
        "Do not save recalled memories again. Save only new user-authorized information with its original source."))
    @server.tool(annotations=ToolAnnotations(read_only_hint=True,open_world_hint=False))
    async def search_memories(query:str,project_id:str|None=None,limit:int=5,mode:str="keyword")->dict:
        """Search authorized memories. project_id accepts a UUID, global, or the exact configured write-project name. Unknown names are rejected; inspect a result's project_id before filtering other projects."""
        if project_id is not None:
            if project_id==config["write_project_name"]:
                project_id=project
            elif project_id!="global":
                try:project_id=str(UUID(project_id))
                except (ValueError,TypeError,AttributeError):
                    raise ToolError("Invalid project_id: use its UUID or the exact configured project name. Unknown project names are never broadened to a library search.") from None
        return await remote("search_memories",{"query":query,"limit":limit,"mode":mode,**({"project_id":project_id} if project_id is not None else {})})
    @server.tool(annotations=ToolAnnotations(read_only_hint=True,open_world_hint=False))
    async def get_memory(memory_id:str,include_history:bool=False)->dict:
        """Read memory and original source; check project, validity and evidence."""
        return await remote("get_memory",{"memory_id":memory_id,"include_history":include_history})
    @server.tool(annotations=ToolAnnotations(read_only_hint=False,destructive_hint=False,open_world_hint=False))
    async def save_memory_candidate(content:str,type:str,source_content:str,idempotency_key:str,decision_key:str|None=None)->dict:
        """Submit new user-authorized information to the configured project as Candidate; local review is required. Source must support content. No activation or completion claim."""
        result=await remote("save_memory",{"project_id":project,"content":content,"type":type,
            "confidence":"Reported","source_content":source_content,"source_title":"豆包工作提交的候选记录",
            "idempotency_key":idempotency_key,**({"decision_key":decision_key} if decision_key else {})},write=True)
        return {**result,"review_required":True,"write_project_id":project,"automatic_capture":False}
    for tool in server._tool_manager._tools.values():
        model=tool.fn_metadata.arg_model
        model.model_config["extra"]="forbid"
        model.model_rebuild(force=True)
        tool.parameters=model.model_json_schema(by_alias=True)
    return server

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--config",type=Path,default=DEFAULT_CONFIG);args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding="utf-8-sig"))
    make_server(config).run(transport="stdio")
