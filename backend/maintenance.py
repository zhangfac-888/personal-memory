from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .db import SCHEMA_VERSION

MARKER = ".personal-memory-data"
ALLOWED_FILES = {MARKER,"memory.db","memory.db-wal","memory.db-shm","admin-key.txt","server.lock","server.stdout.log","server.stderr.log","ollama.stdout.log","ollama.stderr.log"}
ALLOWED_DIRS = {"backups","exports","automation-outbox"}


def mark_data_dir(path: Path):
    marker=path/MARKER
    if not marker.exists():
        marker.write_text("Personal Memory local data directory\n",encoding="utf-8")


def require_data_dir(path: Path):
    path=path.resolve()
    if not path.is_dir() or not (path/MARKER).is_file() or not (path/"memory.db").is_file():
        raise ValueError("目标不是已初始化的 Personal Memory 数据目录")
    return path


def verify_backup(package: Path, destination: Path):
    with zipfile.ZipFile(package) as archive:
        if set(archive.namelist())!={"manifest.json","memory.db"}:
            raise ValueError("备份文件清单不正确")
        manifest=json.loads(archive.read("manifest.json"))
        version=manifest.get("schema_version")
        if manifest.get("format_version")!=1 or not isinstance(version,int) or version<1 or version>SCHEMA_VERSION:
            raise ValueError("备份格式或数据库版本不兼容")
        info=archive.getinfo("memory.db")
        if info.file_size>2*1024*1024*1024:
            raise ValueError("备份数据库超过当前恢复上限")
        h=hashlib.sha256()
        with archive.open("memory.db") as source, destination.open("wb") as target:
            while chunk:=source.read(1024*1024):
                h.update(chunk);target.write(chunk)
        if h.hexdigest()!=manifest.get("sha256"):
            raise ValueError("备份哈希不匹配")
    con=sqlite3.connect(destination)
    from .db import load_vector_extension
    load_vector_extension(con)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("数据库完整性检查失败")
        for table,count in manifest["counts"].items():
            if table not in {"projects","sources","memories","memory_revisions","change_requests",
                             "workspace_aliases","workspace_roots","source_scopes","session_scopes",
                             "automation_event_identity","automation_job_history"} or con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]!=count:
                raise ValueError("备份记录数不匹配")
        con.execute("UPDATE client_credentials SET revoked=1")
        if version >= 4:
            con.execute("UPDATE automation_clients SET revoked=1,enabled=0")
            con.execute("UPDATE automation_jobs SET status='cancelled',error_code='LIBRARY_RESTORED' WHERE status IN ('pending','running','failed')")
        con.commit()
    finally: con.close()
    from .db import Database
    from .service import MemoryService
    MemoryService(Database(destination))
    checkpoint=sqlite3.connect(destination)
    try:
        result=checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0]: raise ValueError("备份索引迁移后的检查点写入失败")
    finally: checkpoint.close()
    return manifest


def restore(package:Path,target:Path,replace:bool=False):
    package=package.resolve();target=target.resolve()
    if not package.is_file(): raise ValueError("备份文件不存在")
    if not replace and target.exists(): raise ValueError("新恢复目录已存在")
    if replace: require_data_dir(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pm-restore-",dir=target.parent) as temporary:
        staged=Path(temporary)/"memory.db"
        manifest=verify_backup(package,staged)
        if replace:
            # The server must be stopped; server.lock is exclusively locked while it runs.
            from .server_lock import ServerLock
            with ServerLock(target/"server.lock"):
                existing=sqlite3.connect(target/"memory.db",timeout=1)
                try:
                    existing.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    before=target/"backups"/("before-restore-"+secrets.token_hex(8)+".db")
                    before.parent.mkdir(exist_ok=True)
                    dst=sqlite3.connect(before)
                    try: existing.backup(dst)
                    finally: dst.close()
                finally: existing.close()
                if (target/"memory.db-wal").exists() or (target/"memory.db-shm").exists():
                    raise ValueError("数据库仍被占用；原数据未被替换")
                os.replace(staged,target/"memory.db")
                (target/"admin-key.txt").write_text(secrets.token_urlsafe(32),encoding="ascii")
        else:
            target.mkdir()
            mark_data_dir(target)
            shutil.copy2(staged,target/"memory.db")
            (target/"admin-key.txt").write_text(secrets.token_urlsafe(32),encoding="ascii")
    return {"restored_to":str(target),"created_at":manifest["created_at"],"counts":manifest["counts"]}


def erase_plan(path:Path):
    path=require_data_dir(path)
    unknown=[p.name for p in path.iterdir() if p.name not in ALLOWED_FILES|ALLOWED_DIRS]
    if unknown: raise ValueError("数据目录含其他文件，拒绝整库清除："+", ".join(unknown))
    paths=[]
    for p in path.iterdir():
        if p.is_file() and p.name!="server.lock": paths.append(p)
        elif p.name in ALLOWED_DIRS:
            allowed_suffix=".zip" if p.name=="backups" else ".json"
            for child in p.iterdir():
                if not child.is_file() or child.suffix!=allowed_suffix and not (p.name=="backups" and child.name.startswith("before-restore-") and child.suffix==".db"):
                    raise ValueError("备份或导出目录有未知文件，拒绝清除："+str(child))
                paths.append(child)
    descriptors=[{"path":str(p.resolve()),"size":p.stat().st_size,"mtime_ns":p.stat().st_mtime_ns} for p in sorted(paths)]
    plan_id=hashlib.sha256(json.dumps({"root":str(path),"files":descriptors},sort_keys=True).encode()).hexdigest()[:16]
    return {"data_dir":str(path),"plan_id":plan_id,"files":descriptors}


def erase(path:Path,plan_id:str):
    from .server_lock import ServerLock
    path=require_data_dir(path)
    with ServerLock(path/"server.lock"):
        plan=erase_plan(path)
        if plan_id!=plan["plan_id"]: raise ValueError("清除清单已变化，请重新预览")
        # Do not recursively delete a computed directory.
        for item in plan["files"]:
            Path(item["path"]).unlink()
        for name in ALLOWED_DIRS:
            folder=path/name
            if folder.exists() and not any(folder.iterdir()): folder.rmdir()
    (path/"server.lock").unlink(missing_ok=True)
    if not any(path.iterdir()): path.rmdir()
    return {"erased":str(path),"files":len(plan["files"])}


def main():
    parser=argparse.ArgumentParser(description="Personal Memory offline maintenance")
    sub=parser.add_subparsers(dest="command",required=True)
    rp=sub.add_parser("restore");rp.add_argument("--backup",type=Path,required=True);rp.add_argument("--target-dir",type=Path,required=True);rp.add_argument("--replace",action="store_true")
    ep=sub.add_parser("erase");ep.add_argument("--data-dir",type=Path,required=True);ep.add_argument("--plan-id")
    args=parser.parse_args()
    try:
        if args.command=="restore": result=restore(args.backup,args.target_dir,args.replace)
        else: result=erase(args.data_dir,args.plan_id) if args.plan_id else erase_plan(args.data_dir)
    except (ValueError,OSError,sqlite3.Error,zipfile.BadZipFile) as exc:
        parser.exit(2,str(exc)+"\n")
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
