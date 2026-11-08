"""Explicit administrator preparation, separate from captured task progress.

Preview uses a SELECT-only connection. Commit never creates projects, grants,
mappings, archive parts or completed steps. First capture attaches the reserved
identity in the same transaction as the original archive.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from .service import Problem, canonical, digest, now
from . import task_contracts, task_checkpoints
from .workspaces import current_client, excluded_projects, effective_exclusions, normalized_path, workspace_identity
from .task_contract_rules import norm_path, path_checks


def _admin(admin):
    if admin.get("kind") != "admin":
        raise Problem("FORBIDDEN", "需要管理权限", 403)


def _definition(data):
    if not isinstance(data, dict) or set(data) != {"project_id", "producer", "consumer", "workspace_root", "contract"}:
        raise Problem("VALIDATION_ERROR", "准备字段无效", 422)
    for role, keys in (("producer", {"client_id", "session_id"}), ("consumer", {"client_id"})):
        actor = data[role]
        if not isinstance(actor, dict) or set(actor) != keys:
            raise Problem("VALIDATION_ERROR", "准备会话身份无效", 422)
        for value in actor.values():
            task_contracts._text(value, 200, "准备会话身份")
    task_contracts._text(data["project_id"], 200, "项目")
    task_contracts._text(data["workspace_root"], 1000, "工作目录")
    if not Path(data["workspace_root"]).is_absolute():
        raise Problem("VALIDATION_ERROR", "准备目录必须是绝对路径", 422)
    contract = _contract(data["contract"])
    return {**data, "contract": contract}


def _contract(data):
    contract = task_contracts.validate(data)
    if "path_roles" not in contract or "step_actors" not in contract:
        raise Problem("VALIDATION_ERROR", "准备必须登记路径角色和完整步骤归属", 422)
    if any(s["state"] != "authorized" or s["basis"] for s in contract["steps"]):
        raise Problem("VALIDATION_ERROR", "新准备不得填入历史完成状态或证据", 422)
    if any(c["evidence"] is not None for c in contract["checks"]):
        raise Problem("VALIDATION_ERROR", "新准备不得填入历史检查证据", 422)
    return contract


def _draft(data):
    """Collect draft omissions without inventing defaults or granting execution."""
    missing, invalid = [], []
    keys = {"project_id", "producer", "consumer", "workspace_root", "contract"}
    if not isinstance(data, dict):
        invalid.append({"field": "definition", "reason": "准备草稿必须是对象"})
        data = {}
    for key in sorted(set(data) - keys):
        invalid.append({"field": key, "reason": "未知准备字段"})

    def text(source, key, limit, field):
        value = source.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
            return None
        try:
            return task_contracts._text(value, limit, field)
        except Problem as error:
            invalid.append({"field": field, "reason": error.message})
            return None

    value = {"project_id": text(data, "project_id", 200, "project_id"),
             "workspace_root": text(data, "workspace_root", 1000, "workspace_root")}
    if value["workspace_root"] and not Path(value["workspace_root"]).is_absolute():
        invalid.append({"field": "workspace_root", "reason": "准备目录必须是绝对路径"})
        value["workspace_root"] = None
    for role, actor_keys in (("producer", ("client_id", "session_id")), ("consumer", ("client_id",))):
        actor = data.get(role)
        if actor is None:
            actor = {}
        elif not isinstance(actor, dict):
            invalid.append({"field": role, "reason": "准备会话身份必须是对象"})
            actor = {}
        for key in sorted(set(actor) - set(actor_keys)):
            invalid.append({"field": role + "." + key, "reason": "未知会话身份字段"})
        value[role] = {key: text(actor, key, 200, role + "." + key) for key in actor_keys}
    contract = data.get("contract")
    if contract is None:
        contract = {}
    elif not isinstance(contract, dict):
        invalid.append({"field": "contract", "reason": "步骤合约必须是对象"})
        contract = {}
    contract_missing = ["contract." + key for key in sorted(task_contracts.CONTRACT_KEYS_V3 - set(contract))]
    missing.extend(contract_missing)
    normalized = None
    if not contract_missing:
        try:
            normalized = _contract(contract)
        except (Problem, TypeError, ValueError) as error:
            invalid.append({"field": "contract", "reason": error.message if isinstance(error, Problem) else "步骤合约格式无效"})
    value["contract"] = normalized
    enforce, mode = contract.get("enforce"), contract.get("execution_mode")
    known = type(enforce) is bool and isinstance(mode, str) and mode in task_contracts.EXECUTION_MODES
    return value, missing, invalid, (enforce and mode == "scoped_write" if known else None)


def task_id(data):
    return "task_" + digest(canonical([data["project_id"], data["producer"]["client_id"], data["producer"]["session_id"]]))[:32]


def root_identity(root):
    path = Path(root).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("root_not_directory")
    stat = path.stat()
    return {"resolved": norm_path(path), "device": stat.st_dev, "inode": stat.st_ino}


def _snapshot(conn, data, *, missing=(), invalid=(), enforcement=None):
    checks, blockers, clients, facts = [], [], {}, {}
    def check(name, good, reason=None):
        status = "unverifiable" if good is None else "pass" if good else "fail"
        checks.append({"id": name, "status": status,
                       **({"reason": reason} if reason and status != "pass" else {})})
        if status != "pass":
            blockers.append(name)
    pid, root = data["project_id"], data["workspace_root"]
    project = conn.execute("SELECT project_id,status,version FROM projects WHERE project_id=?", (pid,)).fetchone() if pid else None
    facts["project"] = dict(project) if project else None
    check("project", bool(project and project["status"] == "active" and pid not in {"global", "personal-sessions"}) if pid else None,
          "project_id_required")
    try:
        if not root:
            raise ValueError("workspace_root_required")
        identity = root_identity(root)
        # Preserve the submitted root instead of silently following a relocated root.
        check("workspace_root", norm_path(Path(root).absolute()) == identity["resolved"], "root_requires_explicit_physical_path")
        facts["root"] = identity
        key, _, _ = workspace_identity(root)
        alias = conn.execute("SELECT project_id FROM workspace_aliases WHERE root_key=?", (key,)).fetchone()
        mapping = conn.execute("SELECT * FROM workspace_directory_mappings WHERE directory=? AND active=1", (normalized_path(root),)).fetchone()
        facts["alias"] = dict(alias) if alias else None
        facts["mapping"] = dict(mapping) if mapping else None
        check("scope", bool(alias and alias["project_id"] == pid and (not mapping or (mapping["project_id"] == pid and mapping["workspace_root"] == normalized_path(root)))) if pid else None,
              "project_id_required")
    except (OSError, RuntimeError, ValueError):
        facts["root"] = None
        check("workspace_root", False if root else None, "root_unavailable" if root else "workspace_root_required")
        check("scope", None, "workspace_root_unavailable")
    for role in ("producer", "consumer"):
        cid = data[role]["client_id"]
        client = None
        try:
            if not cid:
                check(role + "_client", None, "client_id_required")
            else:
                client = current_client(conn, {"client_id": cid})
                clients[role] = client
                check(role + "_client", client["app"] in task_checkpoints.APPS and client["access_mode"] == "library")
        except Problem:
            check(role + "_client", False, "client_unavailable")
        if client and pid and root and facts["root"]:
            exclusions = effective_exclusions(conn, client)
            check(role + "_exclusions", pid not in excluded_projects(conn, client) and not any(normalized_path(root) == x or normalized_path(root).startswith(x + "/") for x in exclusions))
            facts[role] = {"client_id": cid, "app": client["app"], "access_mode": client["access_mode"], "exclusions": exclusions}
        else:
            check(role + "_exclusions", None, "client_project_and_workspace_required")
        grant = conn.execute("SELECT allowed,updated_at FROM task_checkpoint_grants WHERE client_id=? AND project_id=?", (cid, pid)).fetchone() if cid and pid and client else None
        facts[role + "_grant"] = dict(grant) if grant else None
        check(role + "_grant", bool(grant and grant["allowed"]) if cid and pid and client else None,
              "client_and_project_required")
    producer = data["producer"]
    tid = task_id(data) if pid and producer["client_id"] and producer["session_id"] else None
    existing = conn.execute("SELECT * FROM task_preparations WHERE task_id=?", (tid,)).fetchone() if tid else None
    linked = conn.execute("SELECT 1 FROM task_checkpoint_sessions WHERE client_id=? AND session_id=? AND project_id=?",
                          (data["producer"]["client_id"], data["producer"]["session_id"], pid)).fetchone()
    identity_row = conn.execute("SELECT 1 FROM task_checkpoints WHERE task_id=?", (tid,)).fetchone()
    session_preparation = conn.execute("SELECT 1 FROM task_preparations WHERE producer_client_id=? AND producer_session_id=?",
                                       (data["producer"]["client_id"], data["producer"]["session_id"])).fetchone()
    check("identity", not existing and not linked and not identity_row and not session_preparation if tid else None,
          "already_registered" if tid else "project_and_producer_identity_required")
    current = task_contracts._current(conn, tid) if tid else None
    contract = data["contract"]
    check("contract_enforced", contract["enforce"] and contract["execution_mode"] == "scoped_write" if contract else enforcement,
          "contract_enforcement_information_required")
    paths = path_checks({**contract, "workspace_root": root}) if contract and root and facts["root"] else []
    check("paths", all(p["status"] == "pass" for p in paths) if contract and root and facts["root"] else None,
          "valid_contract_and_workspace_required")
    if missing or invalid:
        check("definition", None if missing and not invalid else False, "draft_information_incomplete_or_invalid")
    snapshot = {"definition": data, "facts": facts, "paths": paths,
                "preparation_revision": existing["revision"] if existing else None,
                "contract_version": current["version"] if current else None}
    return {"state": "can_prepare" if not blockers else "blocked", "ready": not blockers,
            "task_id": tid, "workspace_root": root, "checks": checks, "blockers": blockers,
            "paths": paths, "expected": {"preparation_revision": snapshot["preparation_revision"],
                "contract_version": snapshot["contract_version"], "preview_digest": digest(canonical(snapshot))} if not missing and not invalid else None,
            "missing_fields": list(missing), "invalid_fields": list(invalid),
            "writes_performed": False, "root_identity": facts["root"]}


def preview(service, admin, data):
    _admin(admin)
    data, missing, invalid, enforcement = _draft(data)
    with service.db.readonly() as conn:
        return _snapshot(conn, data, missing=missing, invalid=invalid, enforcement=enforcement)


def commit(service, admin, data, expected, reason, idempotency_key):
    _admin(admin)
    data = _definition(data)
    task_contracts._text(reason, 300, "准备原因")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", idempotency_key):
        raise Problem("VALIDATION_ERROR", "幂等编号无效", 422)
    if not isinstance(expected, dict) or set(expected) != {"preparation_revision", "contract_version", "preview_digest"}:
        raise Problem("VALIDATION_ERROR", "准备版本无效", 422)
    definition_hash = digest(canonical(data))
    with service.db.tx() as conn:
        previous = conn.execute("SELECT * FROM task_preparations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if previous:
            if previous["definition_hash"] != definition_hash:
                raise Problem("VERSION_CONFLICT", "幂等编号已用于其他准备内容", 409)
            return {"task_id": previous["task_id"], "revision": previous["revision"], "state": previous["state"],
                    "contract_version": task_contracts._current(conn, previous["task_id"])["version"], "replayed": True}
        result = _snapshot(conn, data)
        if result["expected"] != expected:
            raise Problem("VERSION_CONFLICT", "准备条件已改变，请重新预检", 409)
        if not result["ready"]:
            raise Problem("PREPARATION_BLOCKED", "准备缺项：" + ", ".join(result["blockers"]), 409)
        tid, stamp = result["task_id"], now()
        producer = data["producer"]
        root = normalized_path(data["workspace_root"])
        conn.execute("INSERT INTO task_checkpoints VALUES (?,?,?,?,?,?)",
                     (tid, data["project_id"], producer["client_id"], producer["session_id"], root, stamp))
        conn.execute("INSERT INTO task_preparations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (tid, data["project_id"], producer["client_id"], producer["session_id"], data["consumer"]["client_id"],
                      root, canonical(result["root_identity"]), "reserved", 1, idempotency_key, definition_hash, stamp, stamp))
        version = task_contracts._insert(conn, tid, data["contract"], reason, "admin:" + str(admin.get("id")))
        service._log(conn, admin, "task_prepare", tid)
    return {"task_id": tid, "revision": 1, "state": "reserved", "contract_version": version, "replayed": False}


def cancel(service, admin, tid, expected_revision, reason):
    _admin(admin)
    task_contracts._text(reason, 300, "取消原因")
    if type(expected_revision) is not int:
        raise Problem("VALIDATION_ERROR", "准备版本无效", 422)
    with service.db.tx() as conn:
        row = conn.execute("SELECT * FROM task_preparations WHERE task_id=?", (tid,)).fetchone()
        if not row:
            raise Problem("NOT_FOUND", "准备不可用", 404)
        if row["revision"] != expected_revision:
            raise Problem("VERSION_CONFLICT", "准备已被更新", 409)
        conn.execute("UPDATE task_preparations SET state='cancelled',revision=revision+1,updated_at=? WHERE task_id=?", (now(), tid))
        service._log(conn, admin, "task_prepare_cancel", tid + ":" + reason)
    return {"task_id": tid, "state": "cancelled", "revision": expected_revision + 1}


def decorate(conn, client, task, contract, session_id=None, root=None):
    """Projection for an authorized actor only; unknown callers get no identity."""
    prep = conn.execute("SELECT * FROM task_preparations WHERE task_id=?", (task["task_id"],)).fetchone()
    if not prep:
        return contract
    for bound in {prep["producer_client_id"], prep["consumer_client_id"]}:
        actor_client = current_client(conn, {"client_id": bound})
        allowed = conn.execute("SELECT 1 FROM task_checkpoint_grants WHERE client_id=? AND project_id=? AND allowed=1",
                               (bound, prep["project_id"])).fetchone()
        exclusions = effective_exclusions(conn, actor_client)
        if (not allowed or prep["project_id"] in excluded_projects(conn, actor_client)
                or any(prep["workspace_root"] == x or prep["workspace_root"].startswith(x + "/") for x in exclusions)):
            raise Problem("NOT_FOUND", "任务准备不可用", 404)
    cid = client["client_id"]
    producer = cid == prep["producer_client_id"] and session_id == prep["producer_session_id"]
    consumer = cid == prep["consumer_client_id"] and not producer
    if prep["state"] == "cancelled" or (prep["state"] == "reserved" and not producer) or not (producer or consumer):
        raise Problem("NOT_FOUND", "任务准备不可用", 404)
    if prep["state"] == "captured":
        gate, args = task_checkpoints._parts_gate(conn, client, task["task_id"])
        source = conn.execute("SELECT 1 FROM conversation_parts a JOIN task_checkpoint_sessions ts "
                              "ON a.client_id=ts.client_id AND a.session_id=ts.session_id WHERE ts.task_id=? AND " + gate + " LIMIT 1",
                              [task["task_id"], *args]).fetchone()
        if not source:
            raise Problem("NOT_FOUND", "任务来源不可用", 404)
    blockers = []
    if not root or normalized_path(root) != prep["workspace_root"]:
        blockers.append("workspace_mismatch")
    try:
        if root_identity(prep["workspace_root"]) != json.loads(prep["root_identity_json"]):
            blockers.append("workspace_identity_changed")
    except (OSError, RuntimeError, ValueError):
        blockers.append("workspace_unavailable")
    if not contract or "path_roles" not in contract:
        blockers.append("contract_missing")
        return {"state": "blocked", "preparation_blockers": blockers}
    step = task_contracts.current_step({"steps": contract["steps"]})
    actor = "producer" if producer else "consumer"
    eligible = bool(session_id and step and contract["step_actors"].get(step["step_id"]) == actor)
    if consumer and prep["state"] != "captured":
        eligible = False
    if not eligible:
        blockers.append("step_actor_mismatch" if step else "no_authorized_step")
    if not contract["enforce"]:
        blockers.append("contract_not_enforced")
    if contract["execution_mode"] != "scoped_write":
        blockers.append("contract_not_writable")
    contract = {**contract, "root_identity": json.loads(prep["root_identity_json"])}
    paths = path_checks(contract)
    if any(p["status"] == "fail" for p in paths):
        blockers.append("paths_invalid")
    return {**contract, "preparation_state": prep["state"], "actor": actor, "actor_eligible": eligible,
            "preparation_blockers": blockers, "path_results": paths,
            "deliverable": contract["deliverable"] if not blockers else "准备未就绪：只读核对，不修改文件、不执行命令"}


def for_session(service, client, session_id, root, project_id=None, explicit=None):
    with service.db.readonly() as conn:
        row = conn.execute("SELECT * FROM task_preparations WHERE producer_client_id=? AND producer_session_id=?",
                           (client["client_id"], session_id)).fetchall()
        if not row:
            return None
        if len(row) != 1:
            return {"state": "unavailable"}
        prep = row[0]
        if project_id and prep["project_id"] != project_id:
            return {"state": "unavailable"}
        if prep["state"] == "captured":
            return None
        try:
            client, task = task_checkpoints._row(conn, client, prep["task_id"], allow_reserved=True)
            if explicit and explicit != prep["task_id"]:
                return {"state": "unavailable"}
            value = decorate(conn, client, task, task_contracts.view(task_contracts._current(conn, prep["task_id"]), task["origin_root"]), session_id, root)
        except Problem:
            return {"state": "unavailable"}
        return {"state": "prepared", "task_id": prep["task_id"], "contract": value,
                "readiness": {"state": "ready" if not value.get("preparation_blockers") else "preparation_blocked",
                              "task_id": prep["task_id"], "blockers": value.get("preparation_blockers", []), "contract": value},
                "progress_state": "not_captured", "notice": "仅为管理员预先登记的步骤；没有采集进度或执行完成证据。"}


def attach_capture(conn, client, request, scope):
    rows = conn.execute("SELECT * FROM task_preparations WHERE producer_client_id=? AND producer_session_id=?",
                        (client["client_id"], request.session_id)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise Problem("TASK_CONFLICT", "准备会话身份不唯一", 409)
    prep = rows[0]
    explicit = getattr(request, "task_id", None)
    root = scope.get("workspace_root") or request.workspace_root or request.cwd
    if (prep["state"] == "cancelled" or prep["project_id"] != scope["project_id"]
            or normalized_path(root) != prep["workspace_root"] or (explicit and explicit != prep["task_id"])):
        raise Problem("TASK_CONFLICT", "采集与准备身份冲突；原准备保持不变", 409)
    _, task = task_checkpoints._row(conn, client, prep["task_id"], allow_reserved=True)
    try:
        if root_identity(prep["workspace_root"]) != json.loads(prep["root_identity_json"]):
            raise ValueError()
    except (OSError, RuntimeError, ValueError):
        raise Problem("TASK_CONFLICT", "准备目录身份已改变", 409)
    prior = conn.execute("SELECT task_id FROM task_checkpoint_sessions WHERE client_id=? AND session_id=? AND project_id=?",
                         (client["client_id"], request.session_id, scope["project_id"])).fetchone()
    if prior and prior["task_id"] != prep["task_id"]:
        raise Problem("TASK_CONFLICT", "采集会话已关联其他任务", 409)
    conn.execute("INSERT INTO task_checkpoint_sessions VALUES (?,?,?,?,?) ON CONFLICT(client_id,session_id,project_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                 (client["client_id"], request.session_id, scope["project_id"], prep["task_id"], now()))
    if prep["state"] == "reserved":
        conn.execute("UPDATE task_preparations SET state='captured',revision=revision+1,updated_at=? WHERE task_id=?", (now(), prep["task_id"]))
    return {"state": "prepared_capture" if prep["state"] == "reserved" else "reused", "task_id": prep["task_id"]}
