"""Explicit local installation identity; ordinary test services never discover hosts."""
from pathlib import Path
import json,os,tempfile

def discovery_allowed(data_dir,port,source_root):
    if os.environ.get('PERSONAL_MEMORY_DISABLE_DISCOVERY')=='1':return False
    try:
        data_dir=Path(data_dir).resolve();source_root=Path(source_root).resolve()
        value=json.loads((data_dir/'installation.json').read_text('utf8'))
        return (value.get('format_version')==1 and value.get('discovery_enabled') is True
                and value.get('port')==port and 1024<=port<=65535
                and Path(value['data_root']).resolve()==data_dir
                and Path(value['source_root']).resolve()==source_root
                and (data_dir/'.personal-memory-data').is_file())
    except (OSError,ValueError,KeyError,TypeError):return False

def register_installation(data_dir,port,source_root):
    data_dir=Path(data_dir).resolve();source_root=Path(source_root).resolve()
    if data_dir==Path(data_dir.anchor) or data_dir==Path.home() or not 1024<=port<=65535:
        raise ValueError('Choose a dedicated local data folder and valid port')
    data_dir.mkdir(parents=True,exist_ok=True)
    marker=data_dir/'.personal-memory-data'
    if not marker.exists():marker.write_text('Personal Memory local data directory\n',encoding='utf8')
    value={'format_version':1,'data_root':str(data_dir),'source_root':str(source_root),'port':port,'discovery_enabled':True}
    with tempfile.NamedTemporaryFile(dir=data_dir,delete=False) as f:
        f.write(json.dumps(value,ensure_ascii=False,indent=2).encode('utf8'));temp=f.name
    os.replace(temp,data_dir/'installation.json')
    return value
