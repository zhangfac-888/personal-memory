"""Versioned continuation contract for one granted task checkpoint.

The contract records what is explicitly authorized now (phase, ordered steps,
the single current deliverable, permitted artifacts, progress carrier and
registered checks). Step completion is graded: `reported_complete` comes from
a source report; `observed_complete` needs a recorded tool action plus an
artifact digest. Original archive text is never changed; every version is kept
and any version can be restored as a new version.
"""
from __future__ import annotations
import json
import re
from datetime import datetime
from .service import Problem, canonical, digest, now
from . import task_checkpoints
from .task_contract_rules import (
    CONTRACT_PROFILE, EXECUTION_MODES, STEP_STATES, CARRIERS, safe_relative,
    normalize_command,
)

ACTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CONTRACT_KEYS = {"phase_id", "phase_label", "steps", "allowed_artifacts", "progress_carrier",
                 "forbidden", "checks", "enforce", "notice"}
CONTRACT_KEYS_V2 = CONTRACT_KEYS | {"execution_mode"}
STEP_KEYS = {"step_id", "label", "state", "artifact", "marker", "basis"}
CHECK_KEYS = {"check_id", "command", "cwd", "inputs", "evidence"}
EVIDENCE_KEYS = {"observed_at", "exit_code", "input_digests", "source", "output_sha256"}
RENDER_MAX_BYTES = 1600


def _text(value, limit, field, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise Problem("VALIDATION_ERROR", field + "无效", 422)
    return value


def _ident(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise Problem("VALIDATION_ERROR", field + "编号无效", 422)
    return value


def _normalized(data):
    if not isinstance(data, dict):
        return data
    value = dict(data)
    value.setdefault("execution_mode", "scoped_write")
    return value


def validate(data):
    if (not isinstance(data, dict)
            or (set(data) != CONTRACT_KEYS and set(data) != CONTRACT_KEYS_V2)):
        raise Problem("VALIDATION_ERROR", "任务约定字段无效", 422)
    data = _normalized(data)
    if data["execution_mode"] not in EXECUTION_MODES:
        raise Problem("VALIDATION_ERROR", "任务执行模式无效", 422)
    _ident(data["phase_id"], "阶段")
    _text(data["phase_label"], 120, "阶段说明")
    _text(data["notice"], 600, "约定说明", empty=True)
    if data["progress_carrier"] not in CARRIERS or type(data["enforce"]) is not bool:
        raise Problem("VALIDATION_ERROR", "进度载体或执行开关无效", 422)
    arts = data["allowed_artifacts"]
    if not isinstance(arts, list) or len(arts) > 20 or any(safe_relative(a) != a for a in arts):
        raise Problem("VALIDATION_ERROR", "允许产物必须是项目内规范相对路径", 422)
    forbidden = data["forbidden"]
    if not isinstance(forbidden, list) or len(forbidden) > 10:
        raise Problem("VALIDATION_ERROR", "禁止项无效", 422)
    for item in forbidden:
        _text(item, 200, "禁止项")
    steps = data["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise Problem("VALIDATION_ERROR", "步骤无效", 422)
    seen = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) != STEP_KEYS:
            raise Problem("VALIDATION_ERROR", "步骤字段无效", 422)
        if _ident(step["step_id"], "步骤") in seen:
            raise Problem("VALIDATION_ERROR", "步骤编号重复", 422)
        seen.add(step["step_id"])
        _text(step["label"], 300, "步骤说明")
        if step["state"] not in STEP_STATES:
            raise Problem("VALIDATION_ERROR", "步骤状态无效", 422)
        if step["artifact"] is not None and step["artifact"] not in arts:
            raise Problem("VALIDATION_ERROR", "步骤产物必须在允许产物中", 422)
        if step["marker"] is not None:
            _text(step["marker"], 120, "步骤完成标识")
        if not isinstance(step["basis"], list) or len(step["basis"]) > 8 or any(not isinstance(x, str) or len(x) > 200 for x in step["basis"]):
            raise Problem("VALIDATION_ERROR", "步骤依据无效", 422)
        if step["state"] == "observed_complete" and not step["basis"]:
            raise Problem("VALIDATION_ERROR", "观测完成必须有动作或证据依据", 422)
    checks = data["checks"]
    if not isinstance(checks, list) or len(checks) > 10:
        raise Problem("VALIDATION_ERROR", "检查登记无效", 422)
    for check in checks:
        if not isinstance(check, dict) or set(check) != CHECK_KEYS:
            raise Problem("VALIDATION_ERROR", "检查字段无效", 422)
        _ident(check["check_id"], "检查")
        _text(check["command"], 1000, "检查命令")
        if check["cwd"] is not None and safe_relative(check["cwd"]) != check["cwd"]:
            raise Problem("VALIDATION_ERROR", "检查目录必须是项目内相对路径", 422)
        if not isinstance(check["inputs"], list) or len(check["inputs"]) > 20 or any(safe_relative(x) != x for x in check["inputs"]):
            raise Problem("VALIDATION_ERROR", "检查输入无效", 422)
        evidence = check["evidence"]
        if evidence is not None:
            if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS or type(evidence["exit_code"]) is not int:
                raise Problem("VALIDATION_ERROR", "检查证据无效", 422)
            if evidence["source"] not in {"imported_observation", "post_tool_use"}:
                raise Problem("VALIDATION_ERROR", "检查证据来源无效", 422)
            try:
                if datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError()
            except (AttributeError, TypeError, ValueError):
                raise Problem("VALIDATION_ERROR", "检查时间无效", 422)
            digests = evidence["input_digests"]
            if not isinstance(digests, dict) or set(digests) - set(check["inputs"]) or any(not re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in digests.values()):
                raise Problem("VALIDATION_ERROR", "检查输入摘要无效", 422)
    if len(canonical(data)) > 24000:
        raise Problem("VALIDATION_ERROR", "任务约定过长", 422)
    return data


def _current(conn, task_id):
    row = conn.execute("SELECT * FROM task_contracts WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return None
    return {"task_id": task_id, "version": row["version"], "data": _normalized(json.loads(row["data_json"])),
            "reason": row["reason"], "created_at": row["created_at"], "restored_from": row["restored_from"]}


def _insert(conn, task_id, data, reason, actor, restored_from=None):
    data = _normalized(data)
    current = _current(conn, task_id)
    version = (current["version"] if current else 0) + 1
    conn.execute("INSERT INTO task_contracts VALUES (?,?,?,?,?,?,?)",
                 (task_id, version, canonical(data), reason, actor, now(), restored_from))
    return version


def set_contract(service, admin, task_id, data, expected_version, reason):
    """Admin-authored contract version. expected_version guards concurrent edits."""
    data = validate(data)
    _text(reason, 300, "修改原因")
    if expected_version is not None and type(expected_version) is not int:
        raise Problem("VALIDATION_ERROR", "版本无效", 422)
    with service.db.tx() as conn:
        task_checkpoints._admin_task(conn, admin, task_id)
        current = _current(conn, task_id)
        if (current["version"] if current else None) != expected_version:
            raise Problem("VERSION_CONFLICT", "任务约定已被更新，请先读取最新版本", 409)
        version = _insert(conn, task_id, data, reason, "admin:" + str(admin.get("id")))
        service._log(conn, admin, "task_contract_set", task_id + ":" + str(version))
    return {"task_id": task_id, "version": version}


def restore(service, admin, task_id, version, expected_version):
    """Restore an earlier version as a new version; history stays intact."""
    if type(version) is not int or type(expected_version) is not int:
        raise Problem("VALIDATION_ERROR", "版本无效", 422)
    with service.db.tx() as conn:
        task_checkpoints._admin_task(conn, admin, task_id)
        current = _current(conn, task_id)
        if not current or current["version"] != expected_version:
            raise Problem("VERSION_CONFLICT", "任务约定已被更新，请先读取最新版本", 409)
        old = conn.execute("SELECT data_json FROM task_contracts WHERE task_id=? AND version=?", (task_id, version)).fetchone()
        if not old:
            raise Problem("NOT_FOUND", "约定版本不存在", 404)
        new = _insert(conn, task_id, _normalized(json.loads(old["data_json"])), "restore v" + str(version), "admin:" + str(admin.get("id")), version)
        service._log(conn, admin, "task_contract_restore", task_id + ":" + str(version) + "->" + str(new))
    return {"task_id": task_id, "version": new, "restored_from": version}


def disable(service, admin, task_id, expected_version, reason):
    """Disable a contract as a new version; history and restore targets remain."""
    if type(expected_version) is not int:
        raise Problem("VALIDATION_ERROR", "版本无效", 422)
    _text(reason, 300, "停用原因")
    with service.db.tx() as conn:
        task_checkpoints._admin_task(conn, admin, task_id)
        current = _current(conn, task_id)
        if not current or current["version"] != expected_version:
            raise Problem("VERSION_CONFLICT", "任务约定已被更新，请先读取最新版本", 409)
        data = dict(current["data"])
        data["execution_mode"] = "disabled"
        data["enforce"] = True
        data["notice"] = ("任务约定已由管理员停用；恢复明确版本前不得写入或执行命令。"
                          + (" " + data["notice"] if data.get("notice") else ""))[:600]
        version = _insert(conn, task_id, data, reason, "admin:" + str(admin.get("id")))
        service._log(conn, admin, "task_contract_disable", task_id + ":" + str(version))
    return {"task_id": task_id, "version": version, "execution_mode": "disabled"}


def history(service, admin, task_id):
    with service.db.read() as conn:
        task_checkpoints._admin_task(conn, admin, task_id)
        versions = [{"version": r["version"], "reason": r["reason"], "actor": r["actor"], "created_at": r["created_at"],
                     "restored_from": r["restored_from"], "data": json.loads(r["data_json"])}
                    for r in conn.execute("SELECT * FROM task_contracts WHERE task_id=? ORDER BY version", (task_id,))]
        actions = [dict(r) for r in conn.execute("SELECT * FROM task_contract_actions WHERE task_id=? ORDER BY created_at,action_id", (task_id,))]
    return {"task_id": task_id, "versions": versions, "actions": actions}


def current_step(data):
    for step in data["steps"]:
        if step["state"] in {"reported_complete", "observed_complete"}:
            continue
        return step if step["state"] == "authorized" else None
    return None


def view(contract, origin_root):
    """Client-facing projection; adds derived current step, never new facts."""
    if not contract:
        return None
    data = contract["data"]
    step = current_step(data)
    return {"profile": CONTRACT_PROFILE, "task_id": contract["task_id"], "version": contract["version"],
            "phase_id": data["phase_id"], "phase_label": data["phase_label"], "enforce": data["enforce"],
            "execution_mode": data.get("execution_mode", "scoped_write"),
            "workspace_root": origin_root, "progress_carrier": data["progress_carrier"],
            "allowed_artifacts": data["allowed_artifacts"], "forbidden": data["forbidden"], "notice": data["notice"],
            "steps": [{"step_id": s["step_id"], "label": s["label"], "state": s["state"], "artifact": s["artifact"],
                       "marker": s["marker"], "basis": s["basis"]} for s in data["steps"]],
            "current_step": step["step_id"] if step else None,
            "deliverable": ("任务约定已停用：只读核对，不修改文件、不执行命令"
                            if data.get("execution_mode") == "disabled" else
                            "当前为只读阶段：只核对进度和文件，不修改文件、不执行命令"
                            if data.get("execution_mode") == "read_only" else
                            step["label"] if step else "无剩余授权步骤：只读核对，不修改文件、不重跑检查"),
            "checks": [{"check_id": c["check_id"], "command": c["command"], "cwd": c["cwd"], "inputs": c["inputs"],
                        "evidence": c["evidence"]} for c in data["checks"]]}


def for_client(service, client, task_id):
    with service.db.read() as conn:
        client, task = task_checkpoints._row(conn, client, task_id)
        return view(_current(conn, task_id), task["origin_root"])


def render(value):
    """Compact, model-visible contract. Bounded; details stay behind the route."""
    if not value:
        return ""
    state_names = {"pending": "未授权", "authorized": "已授权待做", "reported_complete": "来源报告完成",
                   "observed_complete": "已观测完成"}
    steps = [{"id": s["step_id"], "state": state_names[s["state"]], "label": s["label"][:80]} for s in value["steps"]]
    body = {"profile": value["profile"], "task_id": value["task_id"], "contract_version": value["version"],
            "execution_mode": value.get("execution_mode", "scoped_write"),
            "phase": value["phase_id"] + " " + value["phase_label"][:60], "current_step": value["current_step"],
            "only_authorized_deliverable": value["deliverable"][:200], "progress_carrier": value["progress_carrier"],
            "allowed_artifacts": value["allowed_artifacts"][:6], "forbidden": [x[:100] for x in value["forbidden"][:4]],
            "steps": steps, "observed_checks": [{"id": c["check_id"], "exit_code": c["evidence"]["exit_code"], "observed_at": c["evidence"]["observed_at"]}
                                                 for c in value["checks"] if c["evidence"]][:4],
            "detail_route": "/automation/v2/task-contracts/" + value["task_id"]}
    carrier = ("进度由 PM 任务检查点自动保存，不要另建本地进度文件。" if value["progress_carrier"] == "pm_checkpoint"
               else "进度写入用户指定的允许产物，不要另建其他进度文件。")
    mode = value.get("execution_mode", "scoped_write")
    mode_note = ("当前执行模式为 read_only：不得修改、创建、删除或移动文件，也不得执行 shell、终端或测试命令。"
                 if mode == "read_only" else
                 "当前任务合约已停用：不得修改文件或执行命令，必须先恢复一个明确版本。"
                 if mode == "disabled" else
                 "当前执行模式为 scoped_write：文件写入必须严格落在 allowed_artifacts。")
    note = ("\n以下是当前任务的续作约定（由用户/管理员登记，版本化，可撤销），是任务边界，不是新的执行许可。"
            + mode_note +
            "已完成步骤不要重做；已观测的检查在相关输入未变化且本轮未要求重测时不要重复执行；"
            + carrier + "只做“唯一授权交付”；没有剩余授权步骤时只读核对。\n")
    def build(b):
        text = json.dumps(b, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
        return note + "<personal_task_contract>" + text + "</personal_task_contract>"
    rendered = build(body)
    while len(rendered.encode("utf8")) > RENDER_MAX_BYTES and len(body["steps"]) > 1:
        body["steps"] = [s for s in body["steps"] if s["id"] == body["current_step"]] or body["steps"][-1:]
        body["steps_truncated"] = True
        rendered = build(body)
    return rendered


# ---- host tool actions -------------------------------------------------------

def _session_task(conn, client, task_id, session_id):
    """The calling session must already be linked to this granted task."""
    client, task = task_checkpoints._row(conn, client, task_id)
    linked = conn.execute("SELECT 1 FROM task_checkpoint_sessions WHERE client_id=? AND session_id=? AND task_id=?",
                          (client["client_id"], session_id, task_id)).fetchone()
    if not linked:
        raise Problem("FORBIDDEN", "本会话未关联此任务", 403)
    return client, task


def _action_payload(payload):
    for key in ("session_id", "tool_use_id", "tool_name"):
        if not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 200:
            raise Problem("VALIDATION_ERROR", "动作字段无效", 422)
    if not ACTION_ID.fullmatch(payload["tool_use_id"]):
        raise Problem("VALIDATION_ERROR", "动作编号无效", 422)
    if payload.get("turn_id") is not None and (not isinstance(payload["turn_id"], str) or len(payload["turn_id"]) > 200):
        raise Problem("VALIDATION_ERROR", "轮次编号无效", 422)
    if payload.get("contract_version") is not None and type(payload["contract_version"]) is not int:
        raise Problem("VALIDATION_ERROR", "合约版本无效", 422)
    if len(canonical(payload.get("tool_input"))) > 400000:
        raise Problem("VALIDATION_ERROR", "动作输入过大", 422)


def _record(conn, row):
    """Idempotent per (task, session, tool_use_id, phase, attempt)."""
    ident = "act_" + digest(canonical([row["task_id"], row["session_id"], row["tool_use_id"], row["phase"], row["attempt"]]))[:32]
    existing = conn.execute("SELECT * FROM task_contract_actions WHERE action_id=?", (ident,)).fetchone()
    if existing:
        return ident, False
    conn.execute("INSERT INTO task_contract_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ident, row["task_id"], row["contract_version"], row["client_id"], row["session_id"], row.get("turn_id"),
                  row["tool_use_id"], row["attempt"], row["phase"], row["tool_name"], row["decision"], row["rule"],
                  row.get("step_id"), row.get("check_id"), row["input_sha256"], row.get("output_sha256"),
                  row.get("exit_code"), row.get("exit_code_basis"), row.get("artifact_json"), now()))
    return ident, True


def pre_tool(service, client, task_id, payload):
    """Deterministic decision for one host tool call, recorded before return."""
    from .task_contract_rules import evaluate
    _action_payload(payload)
    with service.db.read() as conn:
        client, task = _session_task(conn, client, task_id, payload["session_id"])
        contract = _current(conn, task_id)
    value = view(contract, task["origin_root"])
    digests = payload.get("input_digests")
    if digests is not None and (not isinstance(digests, dict) or any(not isinstance(v, str) for v in digests.values())):
        raise Problem("VALIDATION_ERROR", "输入摘要无效", 422)
    decision, reason, detail = evaluate(value, payload["tool_name"], payload.get("tool_input"), payload.get("cwd"),
                                        prompt=payload.get("prompt") or "", input_digests=digests)
    row = {"task_id": task_id, "contract_version": value["version"] if value else None, "client_id": client["client_id"],
           "session_id": payload["session_id"], "turn_id": payload.get("turn_id"), "tool_use_id": payload["tool_use_id"],
           "attempt": int(payload.get("attempt") or 1), "phase": "pre", "tool_name": payload["tool_name"][:160],
           "decision": decision, "rule": detail.get("rule", ""), "check_id": detail.get("check_id"),
           "step_id": value["current_step"] if value else None,
           "input_sha256": digest(canonical(payload.get("tool_input"))), "artifact_json": canonical(detail)}
    audit = "recorded"
    try:
        with service.db.tx(timeout_ms=300) as conn:
            action_id, _ = _record(conn, row)
    except Exception:
        # Audit write failure is reported separately; the rule decision stands.
        action_id, audit = None, "write_failed"
    step = current_step(contract["data"]) if contract else None
    return {"decision": decision, "reason": reason, "rule": detail.get("rule"), "action_id": action_id,
            "audit_state": audit, "contract_version": row["contract_version"],
            "current_step": value["current_step"] if value else None,
            "watch": {"artifact": step["artifact"], "marker": step["marker"]} if step and step["artifact"] and step["marker"] else None}


def post_tool(service, client, task_id, payload):
    """Record what the host reports after a tool ran.

    Exit codes are stored only when the host supplied a structured integer;
    otherwise `unknown`. A step is promoted to observed_complete only when this
    action wrote the step's registered artifact and the hook-read file contains
    the step marker. The promotion is a new contract version with this action
    as basis, so it is visible and restorable like any other edit.
    """
    from .task_contract_rules import file_targets, relative_to, norm_path, SHELL_TOOLS, matches_check
    _action_payload(payload)
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list) or len(artifacts) > 10:
        raise Problem("VALIDATION_ERROR", "产物摘要无效", 422)
    for a in artifacts:
        if not isinstance(a, dict) or set(a) != {"path", "sha256", "contains_marker"} or not re.fullmatch(r"[a-f0-9]{64}", str(a["sha256"])) or type(a["contains_marker"]) is not bool:
            raise Problem("VALIDATION_ERROR", "产物摘要无效", 422)
    exit_code = payload.get("exit_code")
    basis = "host_structured" if type(exit_code) is int else "unknown"
    if basis == "unknown":
        exit_code = None
    output = payload.get("tool_response")
    with service.db.tx(timeout_ms=300) as conn:
        client, task = _session_task(conn, client, task_id, payload["session_id"])
        contract = _current(conn, task_id)
        value = view(contract, task["origin_root"])
        expected_version = payload.get("contract_version")
        version_matches = expected_version is None or (value and value["version"] == expected_version)
        tool = payload["tool_name"].casefold()
        check_id = None
        if value and tool in SHELL_TOOLS and isinstance(payload.get("tool_input"), dict):
            command = payload["tool_input"].get("command")
            check_id = next((c["check_id"] for c in value["checks"] if isinstance(command, str) and matches_check(command, c)), None)
        row = {"task_id": task_id, "contract_version": expected_version if expected_version is not None else (value["version"] if value else None),
               "client_id": client["client_id"],
               "session_id": payload["session_id"], "turn_id": payload.get("turn_id"), "tool_use_id": payload["tool_use_id"],
               "attempt": int(payload.get("attempt") or 1), "phase": "post", "tool_name": payload["tool_name"][:160],
               "decision": "observed", "rule": "post_tool_use" if version_matches else "contract_version_changed",
               "check_id": check_id,
               "step_id": value["current_step"] if value else None,
               "input_sha256": digest(canonical(payload.get("tool_input"))),
               "output_sha256": digest(canonical(output)) if output is not None else None,
               "exit_code": exit_code, "exit_code_basis": basis, "artifact_json": canonical(artifacts)}
        action_id, created = _record(conn, row)
        promoted = None
        if created and value and value["current_step"] and version_matches:
            step = next(s for s in contract["data"]["steps"] if s["step_id"] == value["current_step"])
            targets = file_targets(payload["tool_name"], payload.get("tool_input")) or []
            written = {relative_to(t if re.match(r"^[A-Za-z]:[\\/]|^/", t) else str(payload.get("cwd") or task["origin_root"]) + "/" + t,
                                   task["origin_root"]) for t in targets}
            artifact = norm_path(step["artifact"]) if step["artifact"] else None
            hit = artifact and step["marker"] and artifact in written and any(
                norm_path(a["path"]) == artifact and a["contains_marker"] for a in artifacts)
            if hit:
                data = json.loads(canonical(contract["data"]))
                for s in data["steps"]:
                    if s["step_id"] == step["step_id"]:
                        s["state"] = "observed_complete"
                        s["basis"] = (s["basis"] + [action_id])[-8:]
                version = _insert(conn, task_id, data, "observed " + step["step_id"] + " via " + action_id,
                                  "auto:" + client["client_id"])
                promoted = {"step_id": step["step_id"], "contract_version": version}
                service._log(conn, {"kind": "automation", "id": "auto:" + client["client_id"]}, "task_step_observed",
                             task_id + ":" + step["step_id"] + ":" + str(version))
    return {"action_id": action_id, "recorded": created, "exit_code_basis": basis, "promoted": promoted,
            "contract_version": expected_version, "version_match": version_matches}
