"""Add the official DSH MCP client with a separate read-only library identity.

The token stays in the Windows credential environment; the owned YAML block
contains only executable paths and its variable name. Automatic capture keeps
its own identity. This installs transport, not proof of model tool adoption.
"""
from __future__ import annotations
import argparse,hashlib,http.cookiejar,json,os,re,sys,tempfile,urllib.parse,urllib.request,uuid,winreg
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PROFILE_ROOT=Path.home()/'.dsh'
ENV_NAME='PERSONAL_MEMORY_MCP_DSH_TOKEN'
ENV_STORE=None
BEGIN='# BEGIN PERSONAL_MEMORY_DSH_MCP'
END='# END PERSONAL_MEMORY_DSH_MCP'
ENTRY='personal-memory-mcp'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    raw=value if isinstance(value,bytes) else (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(raw);temporary=Path(f.name)
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def env_get():
    if ENV_STORE is not None:return ENV_STORE.get(ENV_NAME)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:return winreg.QueryValueEx(key,ENV_NAME)[0]
    except FileNotFoundError:return None

def env_set(value):
    if ENV_STORE is not None:
        if value is None:ENV_STORE.pop(ENV_NAME,None)
        else:ENV_STORE[ENV_NAME]=value
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
        if value is None:
            try:winreg.DeleteValue(key,ENV_NAME)
            except FileNotFoundError:pass
        else:winreg.SetValueEx(key,ENV_NAME,0,winreg.REG_SZ,value)

def plugin_config(base):
    python=ROOT/'.venv/Scripts/python.exe'
    if not python.is_file():python=Path(sys.executable).resolve()
    bridge=ROOT/'workbuddy_readonly_bridge.py'
    if not bridge.is_file():raise ValueError('Read-only bridge is missing')
    return {'transport':'stdio','serverName':'personal_memory','command':str(python),
        'args':['-X','utf8',str(bridge),'--token-env',ENV_NAME,'--url',base+'/mcp/'],
        'toolCallTimeoutMs':30000,'failOnStartupError':False}

def owned(raw):
    text=raw.decode('utf-8-sig')
    if BEGIN not in text and END not in text:return None
    if text.count(BEGIN)!=1 or text.count(END)!=1 or text.index(BEGIN)>text.index(END):raise ValueError('Malformed owned MCP block')
    return text.split(BEGIN,1)[1].split(END,1)[0]

def merge_patch(raw,config):
    text=raw.decode('utf-8-sig') if raw else ''
    if owned(raw) is not None:text=text[:text.index(BEGIN)]+text[text.index(END)+len(END):]
    if re.search(r'(?m)^\s*(?:-\s*)?id:\s*[\x22\x27]?'+ENTRY+r'\b',text) or re.search(r'(?m)^\s*serverName:\s*[\x22\x27]?personal_memory\b',text):
        raise ValueError('Unowned personal memory MCP entry exists; preserve it')
    stripped='\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#')).strip()
    if stripped.startswith('['):
        values=json.loads(stripped)
        if values!=[]:raise ValueError('Nonempty JSON profile needs explicit migration')
        text='\n'.join(line for line in text.splitlines() if line.lstrip().startswith('#'))
    row={'id':ENTRY,'name':'@deepseek-ai/dsh-mcp-client','config':config}
    block=BEGIN+'\n- insert:\n    - '+json.dumps(row,ensure_ascii=False)+'\n'+END+'\n'
    return (text.rstrip()+'\n\n'+block).lstrip('\n').encode('utf-8')

def connect(base,data):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def api(path,body=None,csrf=None,method=None):
        headers={'Content-Type':'application/json','Origin':base,'x-personal-memory-installer':'local-installer'}
        if csrf:headers['x-csrf-token']=csrf;headers['Idempotency-Key']=str(uuid.uuid4())
        req=urllib.request.Request(base+path,data=json.dumps(body).encode('utf-8') if body is not None else None,headers=headers,method=method)
        with opener.open(req,timeout=15) as response:return json.load(response)
    csrf=api('/api/session',{'key':(data/'admin-key.txt').read_text(encoding='ascii').strip()})['csrf_token']
    return api,csrf

def install(base,data,profiles=('desktop',),profile_root=None):
    base=base.rstrip('/');address=urllib.parse.urlparse(base);data=Path(data).resolve()
    if address.scheme!='http' or address.hostname!='127.0.0.1' or address.path or address.query or address.fragment or address.username or address.password:raise ValueError('Only a loopback memory service is allowed')
    if not (data/'.personal-memory-data').is_file():raise ValueError('Unmarked data directory')
    home=Path(profile_root) if profile_root else PROFILE_ROOT
    if home.resolve()!=PROFILE_ROOT.resolve() and ENV_STORE is None:raise ValueError('Isolated profile requires isolated credentials')
    if not profiles or len(set(profiles))!=len(profiles) or any(not re.fullmatch('[a-z][a-z0-9-]{0,63}',p) for p in profiles):raise ValueError('Invalid DSH profile')
    targets=[home/'profiles'/p/'cordis.patch.yml' for p in profiles]
    if any(not p.parent.is_dir() for p in targets):raise ValueError('Initialize the intended DSH profile first')
    folder=data/'connectors/dsh';manifest_path=folder/'mcp-manifest.json';old_manifest=read(manifest_path);old_token=env_get()
    if bool(old_manifest)!=bool(old_token):raise ValueError('Incomplete MCP installation; inspect before replacing')
    if old_manifest and (old_manifest.get('base_url')!=base or old_manifest.get('data_marker')!=str(data/'.personal-memory-data')):raise ValueError('Refusing credential redirection')
    automatic=read(folder/'config.json')
    if automatic and (automatic.get('base_url')!=base or automatic.get('app')!='dsh' or automatic.get('data_marker')!=str(data/'.personal-memory-data')):raise ValueError('Automatic connector points elsewhere')
    previous={p:p.read_bytes() if p.exists() else b'' for p in targets}
    for p,raw in previous.items():
        block=owned(raw)
        if block is not None and (not old_manifest or old_manifest.get('owned_blocks',{}).get(str(p))!=block):raise ValueError('Owned MCP block was edited; preserve it')
    config=plugin_config(base);merged={p:merge_patch(raw,config) for p,raw in previous.items()}
    api,csrf=connect(base,data);created=None;written={};old_raw=manifest_path.read_bytes() if manifest_path.exists() else None
    try:
        # Use the same root exclusions as automatic capture, with a separate read-only credential.
        if not old_manifest:
            created=api('/api/automation/library-clients',{'app':'dsh','excluded_roots':automatic.get('excluded_roots',[]),'purpose':'mcp'},csrf)
            token=created.pop('token');client_id=created['client_id']
        else:token=old_token;client_id=old_manifest['client_id']
        env_set(token)
        for path,raw in previous.items():
            if raw:write(folder/'backups'/('mcp-'+path.parent.name+'-'+uuid.uuid4().hex+'.yml'),raw)
        for path,content in merged.items():
            if (path.read_bytes() if path.exists() else b'')!=previous[path]:raise ValueError('DSH profile changed during installation')
            write(path,content);written[path]=content
        manifest={'host':'dsh','mode':'library-read-only','purpose':'mcp','base_url':base,'data_marker':str(data/'.personal-memory-data'),'client_id':client_id,'token_env':ENV_NAME,
            'profile_targets':[str(p) for p in targets],'owned_blocks':{str(p):owned(raw) for p,raw in merged.items()},'bridge_sha256':hashlib.sha256((ROOT/'workbuddy_readonly_bridge.py').read_bytes()).hexdigest()}
        write(manifest_path,manifest)
        return {'host':'dsh','mode':'library-read-only','client_id':client_id,'profile_targets':[str(p) for p in targets],'idempotent':bool(old_manifest) and all(previous[p]==merged[p] for p in targets),'model_adoption':'requires real host evidence'}
    except Exception:
        for path,content in written.items():
            if path.exists() and path.read_bytes()==content:write(path,previous[path])
        if old_raw is None:manifest_path.unlink(missing_ok=True)
        else:write(manifest_path,old_raw)
        env_set(old_token)
        if created:api('/api/automation/clients/'+created['client_id'],{'enabled':False,'revoke':True},csrf,'PATCH')
        raise
    finally:api('/api/session',csrf=csrf,method='DELETE')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8765');parser.add_argument('--data',type=Path,default=Path(str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")));parser.add_argument('--profile',action='append')
    args=parser.parse_args();print(json.dumps(install(args.base,args.data,args.profile or ['desktop']),ensure_ascii=False))
if __name__=='__main__':main()