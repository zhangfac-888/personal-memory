"""Exact PM hook output audit. It does not claim host/model request visibility."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

RETENTION_SECONDS = 7 * 86400


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        stream.write(canonical(value).encode("utf-8"))
        temporary=Path(stream.name)
    try:
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


class AuditedOutput(dict):
    def __init__(self, value, path, receipt):
        super().__init__(value)
        self.audit_path=path
        self.audit_receipt=receipt


def directory(config):
    if config.get("injection_audit_enabled") is not True:
        return None
    if config.get("injection_audit_dir"):
        return Path(config["injection_audit_dir"])
    marker=config.get("data_marker")
    if not marker:
        return None
    # Client identities isolate records without exposing their token or allowing path traversal.
    label=str(config.get("app") or "hook")
    label=label if label.isascii() and label.replace("-", "").isalnum() else "hook"
    return Path(marker).parent/"injection-audit"/label


def retention_seconds(config):
    hours=config.get("injection_audit_retention_hours",168)
    if type(hours) is not int or not 1<=hours<=720:raise ValueError("Audit retention hours must be1..720")
    return hours*3600


def cleanup(folder, timestamp, retention=RETENTION_SECONDS):
    marker=folder/"_maintenance.json"
    previous={}
    if marker.exists():
        try:previous=json.loads(marker.read_text(encoding="utf-8"))
        except (OSError,ValueError):pass
    if marker.exists() and timestamp-marker.stat().st_mtime<3600 and previous.get("retention_seconds")==retention:
        return
    # Only our flat audit records are eligible; never follow links or delete arbitrary folders.
    for old in folder.glob("audit-*.json"):
        if old.is_symlink() or not old.is_file():
            continue
        if old.stat().st_mtime<timestamp-retention:
            old.unlink(missing_ok=True)
    atomic(marker,{"last_cleanup_epoch":timestamp,"retention_seconds":retention})


def retain(config, event, service_result, host_output):
    folder=directory(config)
    if folder is None:
        return host_output
    folder.mkdir(parents=True,exist_ok=True)
    timestamp=time.time()
    retention=retention_seconds(config)
    cleanup(folder,timestamp,retention)
    ident="audit-"+str(time.time_ns())+"-"+uuid.uuid4().hex
    path=folder/(ident+".json")
    context=service_result.get("context_text", "")
    if not isinstance(context,str):
        raise ValueError("Context is not text")
    protocol=canonical(host_output)+"\n"
    receipt={"audit_id":ident,"context_sha256":digest(context),"context_chars":len(context),
        "protocol_sha256":digest(protocol),"boundary":"pm_hook_output_before_host_processing"}
    result={key:service_result[key] for key in ("context_text","items","scope","trace_id","task_handoff",
        "warnings","retrieval_coverage","conversation_references","conversation_search_state") if key in service_result}
    atomic(path,{"schema_version":1,"recorded_at_epoch":timestamp,"expires_at_epoch":timestamp+retention,
        "retention_cleanup":"on next audited read, checked at most hourly; inactive service is not a deletion timer",
        "session_hash":digest(str(event.get("session_id", ""))),"turn_hash":digest(str(event.get("turn_id", ""))),
        "hook_event":event.get("hook_event_name"),"client_app":config.get("app"),
        "trace_id":service_result.get("trace_id"),"state":"prepared_for_stdout",**receipt,
        "injection_text":context,"service_evidence":result,"host_output":host_output,
        "sensitivity":"plaintext memory/source bodies and project information; no authentication headers are collected",
        "host_final_model_request_observed":False})
    return AuditedOutput(host_output,path,receipt)


def confirm_stdout(output):
    if not isinstance(output,AuditedOutput):
        return
    saved=json.loads(output.audit_path.read_text(encoding="utf-8"))
    assert saved["protocol_sha256"]==digest(canonical(output)+"\n")
    saved.update(state="written_to_hook_stdout",stdout_written_at_epoch=time.time())
    atomic(output.audit_path,saved)


def main():
    """Local-only maintenance. Preview is default; changes require --apply."""
    import argparse
    parser=argparse.ArgumentParser(description="PM injection audit controls and offline cleanup")
    parser.add_argument("--config",required=True)
    mode=parser.add_mutually_exclusive_group();mode.add_argument("--disable",action="store_true");mode.add_argument("--enable",action="store_true")
    parser.add_argument("--retention-hours",type=int)
    parser.add_argument("--cleanup",action="store_true");parser.add_argument("--purge",action="store_true")
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if args.purge and not args.cleanup:parser.error("--purge requires --cleanup")
    path=Path(args.config);config=json.loads(path.read_text(encoding="utf-8-sig"));changes={}
    if args.disable:changes["injection_audit_enabled"]=False
    if args.enable:changes["injection_audit_enabled"]=True
    if args.retention_hours is not None:changes["injection_audit_retention_hours"]=args.retention_hours
    updated={**config,**changes};retention=retention_seconds(updated)
    folder=directory({**updated,"injection_audit_enabled":True})
    candidates=[]
    if args.cleanup and folder and folder.exists():
        if folder.is_symlink():raise ValueError("Refuse a linked audit directory")
        resolved=folder.resolve()
        for file in folder.glob("audit-*.json"):
            if file.is_symlink() or not file.is_file() or file.resolve().parent!=resolved:continue
            if args.purge or file.stat().st_mtime<time.time()-retention:candidates.append(file)
    if args.apply:
        if changes:atomic(path,updated)
        for file in candidates:file.unlink(missing_ok=True)
    print(canonical({"mode":"applied" if args.apply else "preview","audit_directory":str(folder) if folder else None,
        "configuration_changes":changes,"enabled":updated.get("injection_audit_enabled",False),
        "retention_hours":retention//3600,"cleanup_candidates":len(candidates),"deleted":len(candidates) if args.apply else 0,
        "scope":"only audit-*.json regular files in the selected configured directory; no recursion"}))


if __name__=="__main__":main()
