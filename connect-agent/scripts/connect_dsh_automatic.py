"""DSH Cordis adapter installer; application code stays under the existing D-drive service."""
from __future__ import annotations
import argparse,hashlib,http.cookiejar,json,os,re,sys,tempfile,urllib.request,urllib.parse,uuid,winreg
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PROFILE_ROOT=Path.home()/'.dsh'
ENV_STORE=None
ENV_NAME='PERSONAL_MEMORY_AUTO_DSH_TOKEN'
PYTHON=ROOT/'.venv/Scripts/python.exe'
if not PYTHON.is_file():PYTHON=Path(sys.executable)
HOOK=ROOT/'connect-agent/runtime/hook.py'
BEGIN='# BEGIN PERSONAL_MEMORY_DSH_AUTOMATIC'
END='# END PERSONAL_MEMORY_DSH_AUTOMATIC'

def read(p):return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
def write(p,value):
    p.parent.mkdir(parents=True,exist_ok=True)
    content=value if isinstance(value,bytes) else (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf8')
    with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:f.write(content);temp=Path(f.name)
    try:os.replace(temp,p)
    finally:temp.unlink(missing_ok=True)
def env_get():
    if ENV_STORE is not None:return ENV_STORE.get(ENV_NAME)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as k:return winreg.QueryValueEx(k,ENV_NAME)[0]
    except FileNotFoundError:return None
def env_set(value):
    if ENV_STORE is not None:
        if value is None:ENV_STORE.pop(ENV_NAME,None)
        else:ENV_STORE[ENV_NAME]=value
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as k:
        if value is None:
            try:winreg.DeleteValue(k,ENV_NAME)
            except FileNotFoundError:pass
        else:winreg.SetValueEx(k,ENV_NAME,0,winreg.REG_SZ,value)
def merge_patch(original,plugin):
    text=original.decode('utf-8-sig') if original else ''
    if BEGIN in text or END in text:
        if text.count(BEGIN)!=1 or text.count(END)!=1 or text.index(BEGIN)>text.index(END):raise ValueError('Invalid owned DSH patch markers')
        text=text[:text.index(BEGIN)]+text[text.index(END)+len(END):]
    elif re.search(r'(?m)^\s*(?:-\s*)?id:\s*[\x22\x27]?personal-memory-automatic\b',text):
        raise ValueError('An unowned DSH memory entry exists; preserve it')
    # Non-empty JSON arrays cannot be followed by a YAML list; keep every value.
    stripped='\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#')).strip()
    row={'insert':[{'id':'personal-memory-automatic','name':str(plugin)}]}
    if stripped.startswith('['):
        items=json.loads(stripped)
        if not isinstance(items,list):raise ValueError('DSH patch must be a list')
        if any('personal-memory-automatic'==v.get('id') for v in items if isinstance(v,dict)):raise ValueError('Unowned DSH memory row')
        if items:raise ValueError('A nonempty JSON patch needs explicit migration; preserving it')
        text='\n'.join(line for line in text.splitlines() if line.lstrip().startswith('#'))
    block=BEGIN+'\n- insert:\n    - id: personal-memory-automatic\n      name: '+json.dumps(str(plugin),ensure_ascii=False)+'\n'+END+'\n'
    return (text.rstrip()+'\n\n'+block).lstrip('\n').encode('utf8')
def install(base,data,profiles=('desktop',),profile_root=None):
    base=base.rstrip('/');data=Path(data);profile_root=Path(profile_root) if profile_root else PROFILE_ROOT
    url=urllib.parse.urlparse(base)
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password:raise ValueError('Memory service must be loopback')
    if not profiles or any(not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',p) for p in profiles):raise ValueError('Invalid profile')
    folder=data/'connectors/dsh';config_path=folder/'config.json';manifest_path=folder/'manifest.json';plugin=folder/'personal-memory.mjs'
    targets=[profile_root/'profiles'/p/'cordis.patch.yml' for p in profiles]
    if any(not p.parent.is_dir() for p in targets):raise ValueError('Initialize the intended DSH profile before installation')
    previous={p:p.read_bytes() if p.exists() else b'' for p in targets}
    # Validate every target before issuing a credential or changing any file.
    merged={p:merge_patch(raw,plugin) for p,raw in previous.items()}
    old_manifest=read(manifest_path);old_config=read(config_path);existing_token=env_get()
    if bool(old_manifest)!=bool(old_config) or bool(old_config)!=bool(existing_token):raise ValueError('Incomplete DSH installation; inspect before replacing')
    if old_config and (old_config.get('base_url')!=base or old_config.get('app')!='dsh' or old_config.get('data_marker')!=str(data/'.personal-memory-data')):raise ValueError('DSH target changed; refusing credential redirection')
    if old_manifest and plugin.exists() and hashlib.sha256(plugin.read_bytes()).hexdigest()!=old_manifest['plugin_sha256']:raise ValueError('Owned DSH plugin was edited; preserve it')
    # Existing owned profile content must match the last installed patch block.
    for p,raw in previous.items():
        if BEGIN.encode() in raw and old_manifest:
            prior=old_manifest.get('owned_blocks',{}).get(str(p))
            actual=raw.decode('utf-8-sig').split(BEGIN,1)[1].split(END,1)[0]
            if prior is None or actual!=prior:raise ValueError('Owned DSH patch block was edited; preserve it')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def api(path,body=None,csrf=None,method=None):
        headers={'Content-Type':'application/json','Origin':base,'x-personal-memory-installer':'local-installer'}
        if csrf:headers['x-csrf-token']=csrf;headers['Idempotency-Key']=str(uuid.uuid4())
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        with opener.open(req,timeout=15) as r:return json.load(r)
    csrf=api('/api/session',{'key':(data/'admin-key.txt').read_text().strip()})['csrf_token']
    created=None;changed=False;written={}
    old_files={p:p.read_bytes() if p.exists() else None for p in (config_path,manifest_path,plugin)}
    try:
        if not old_config:
            created=api('/api/automation/library-clients',{'app':'dsh','excluded_roots':[],'purpose':'automation'},csrf)
            token=created.pop('token');client_id=created['client_id']
        else:token=existing_token;client_id=old_config['client_id']
        config={'enabled':True,'app':'dsh','base_url':base,'client_id':client_id,'access_mode':'library','token_env':ENV_NAME,'roots':[],'excluded_roots':[],
            'state_dir':str(folder/'state'),'outbox_dir':str(data/'automation-outbox'/client_id),'data_marker':str(data/'.personal-memory-data'),'transcript_roots':[]}
        template=(ROOT/'connect-agent/adapters/dsh.mjs').read_text(encoding='utf-8-sig')
        for marker,value in [('__PM_PYTHON__',PYTHON),('__PM_HOOK__',HOOK),('__PM_CONFIG__',config_path)]:template=template.replace(marker,json.dumps(str(value)))
        generated=template.encode('utf8')
        folder.mkdir(parents=True,exist_ok=True)
        for p,raw in previous.items():
            if raw:
                backup=folder/'backups'/(p.parent.name+'-before-'+uuid.uuid4().hex+'.yml');write(backup,raw)
        env_set(token);changed=True
        write(config_path,config);write(plugin,generated)
        for p,content in merged.items():
            if (p.read_bytes() if p.exists() else b'')!=previous[p]:raise ValueError('DSH profile changed during installation')
            write(p,content);written[p]=content
        manifest={'host':'dsh','client_id':client_id,'plugin':str(plugin),'plugin_sha256':hashlib.sha256(generated).hexdigest(),'profile_targets':[str(p) for p in targets],
            'owned_blocks':{str(p):v.decode().split(BEGIN,1)[1].split(END,1)[0] for p,v in merged.items()}}
        write(manifest_path,manifest)
        return {'host':'dsh','configuration_saved':True,'automatic_client_id':client_id,'access_mode':'library','plugin':str(plugin),'profile_targets':[str(p) for p in targets],'idempotent':bool(old_config) and all(previous[p]==merged[p] for p in targets)}
    except Exception:
        for p,ours in written.items():
            if p.exists() and p.read_bytes()==ours:write(p,previous[p])
        for p,raw in old_files.items():
            if raw is None:p.unlink(missing_ok=True)
            else:write(p,raw)
        if changed:env_set(existing_token)
        if created:api('/api/automation/clients/'+created['client_id'],{'enabled':False,'revoke':True},csrf,'PATCH')
        raise
    finally:
        api('/api/session',csrf=csrf,method='DELETE')
def status(data):
    config=read(Path(data)/'connectors/dsh/config.json');manifest=read(Path(data)/'connectors/dsh/manifest.json')
    return {'host':'dsh','configuration_saved':bool(config and manifest),'token_present':bool(env_get()),'client_id':config.get('client_id'),'profiles':manifest.get('profile_targets',[]),'actual_use':'requires host evidence'}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['status','install']);parser.add_argument('--base',default='http://127.0.0.1:8765');parser.add_argument('--data',type=Path,default=Path(str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data")));parser.add_argument('--profile',action='append')
    args=parser.parse_args();print(json.dumps(status(args.data) if args.command=='status' else install(args.base,args.data,args.profile or ['desktop']),ensure_ascii=False))
if __name__=='__main__':main()
