"""Deterministic continuation-contract rules shared by the hook and the server.

Dependency-free on purpose: the host hook imports this module. Decisions cover
only registered checks and structured file paths; everything else is reported
as not covered instead of being guessed from command similarity.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

CONTRACT_PROFILE = "task-contract-v2"
STEP_STATES = {"pending", "authorized", "reported_complete", "observed_complete"}
CARRIERS = {"pm_checkpoint", "user_file"}
EXECUTION_MODES = {"scoped_write", "read_only", "disabled"}
# Names that a receiver commonly invents for local progress notes.
PROGRESS_NAME = re.compile(r"^(?:progress|handoff|进度|交接|接力)[\w\-. （）()]*\.(?:md|markdown|txt|json|ya?ml)$"
                           r"|^[\w\-. ]*[-_.](?:progress|handoff)\.(?:md|markdown|txt|json|ya?ml)$", re.I)
RETEST = re.compile(r"重新(?:检查|测试|跑|运行|验证|执行)|重测|重跑|复测|再(?:跑|测|检查|验证)(?:一[遍次下])?|"
                    r"\b(?:re-?run|re-?test|run (?:the )?(?:tests?|checks?) again)\b", re.I)
FILE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch"}
SHELL_TOOLS = {"bash", "powershell", "shell", "exec_command", "local_shell"}
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
SHELL_WRITE = re.compile(r"(?:>>?|\b(?:tee|Set-Content|Add-Content|Out-File|New-Item|Copy-Item|Move-Item|cp|mv|copy|move)\b)"
                         r"[^|;&\n]*?([^\s'\"|;&<>]+\.(?:md|markdown|txt|json|ya?ml))", re.I)
SHELL_MUTATION = re.compile(r"(?:>>?|\b(?:tee|Set-Content|Add-Content|Out-File|New-Item|Copy-Item|Move-Item|"
                            r"Remove-Item|Rename-Item|cp|mv|rm|copy|move|del|erase|mkdir|md)\b)", re.I)
HANDOFF_PROMPT = re.compile(
    r"(?:^|\s)(?:接手|恢复|承接|交接|接力)(?:这个|当前|上述|之前|刚才|原|此)?"
    r"(?:项目|任务|工作|进度|文档|验收)?"
    r"|(?:继续|接着)(?:\s*(?:workbuddy|codex|claude(?:\s+code)?|豆包))?"
    r"(?:\s*[^，。！？!?\n]{0,30})?(?:项目|任务|工作|进度|文档|验收)"
    r"|核对.{0,30}(?:任务|进度)"
    r"|\b(?:resume|continue)\b.{0,30}\b(?:task|work|project|progress|handoff)\b",
    re.I,
)
DIRECT_RESUME_OBJECT = re.compile(
    r"(?:请|帮我)?(?:继续|接着|恢复|承接|接手|交接|接力)"
    r"(?:\s*(?:workbuddy|codex|claude(?:\s+code)?|豆包))?"
    r"(?:\s*的)?\s*(?:这个|当前|上述|之前|刚才|原|此)?"
    r"(?:项目|任务|工作|进度|文档|验收)"
    r"|\b(?:resume|continue)\b"
    r"(?:\s+(?:workbuddy|codex|claude(?:\s+code)?))?"
    r"(?:\s+(?:this|the current|the previous|the original))?"
    r"\s+(?:task|work|project|progress|handoff)\b",
    re.I,
)
BARE_RESUME = re.compile(
    r"^\s*(?:请|帮我)?(?:继续|接着|恢复|承接|接手|resume|continue)\s*[。.!！]?\s*$",
    re.I,
)
EXPLANATORY_CONTINUE = re.compile(
    r"(?:请|帮我)?(?:继续|接着)(?:\s*)(?:说明|解释|介绍|回答|讨论|分析|展开)",
    re.I,
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def normalize_command(command):
    """Whitespace-only normalization. Different spellings stay different."""
    return " ".join(str(command).split())


def norm_path(value):
    return str(value).replace("\\", "/").rstrip("/").casefold()


def safe_relative(value):
    """Validated artifact/input path: relative, no traversal, forward slashes."""
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        return None
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
        return None
    parts = PurePosixPath(text).parts
    if any(p in {"..", ""} for p in parts):
        return None
    return "/".join(p for p in parts if p != ".") or "."


def is_progress_file(path, carrier, allowed_artifacts):
    """A local progress note the contract did not authorize."""
    if carrier == "user_file":
        return False
    name = PurePosixPath(norm_path(path)).name
    if not PROGRESS_NAME.match(name):
        return False
    rel = norm_path(path)
    return not any(rel == norm_path(a) or rel.endswith("/" + norm_path(a)) for a in allowed_artifacts)


def file_targets(tool_name, tool_input):
    """Structured write targets, or None when the input cannot be parsed."""
    tool = str(tool_name or "").casefold()
    if not isinstance(tool_input, dict):
        return None
    if tool == "apply_patch":
        patch = tool_input.get("command") or tool_input.get("input") or tool_input.get("patch")
        if not isinstance(patch, str):
            return None
        return [a or b for a, b in PATCH_PATH.findall(patch)]
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(tool_input.get(key), str):
            return [tool_input[key]]
    return None


def relative_to(path, root):
    """Workspace-relative POSIX path, or None when outside the root."""
    p = norm_path(path)
    r = norm_path(root)
    if not re.match(r"^[a-z]:/|^/", p):
        return safe_relative(str(path))
    if p == r:
        return "."
    if p.startswith(r + "/"):
        return p[len(r) + 1:]
    return None


def matches_check(command, check):
    return normalize_command(command).casefold() == normalize_command(check["command"]).casefold()


def is_handoff_prompt(prompt):
    """Recognize explicit task continuation without treating every '继续' as one."""
    text = str(prompt or "")
    if EXPLANATORY_CONTINUE.search(text):
        return False
    return bool(BARE_RESUME.search(text) or HANDOFF_PROMPT.search(text))


def is_resume_prompt(prompt):
    """Narrower signal for attaching to an existing task, not task-word mentions."""
    text = str(prompt or "")
    if BARE_RESUME.search(text):
        return True
    # A handoff prompt may include background before the actual request. Once
    # that background is present, requiring the whole text to start with
    # "继续/接手" makes readiness and task selection disagree.
    if DIRECT_RESUME_OBJECT.search(text):
        return True
    if EXPLANATORY_CONTINUE.search(text):
        return False
    return bool(
        HANDOFF_PROMPT.search(text)
        and re.match(
            r"^\s*(?:请|帮我)?(?:继续|接着|接手|恢复|承接|交接|接力|resume|continue)",
            text,
            re.I,
        )
    )


def _artifact_relative(target, cwd, root):
    text = str(target or "")
    absolute = text if re.match(r"^[A-Za-z]:[\\/]|^/", text) else (
        str(cwd or root).rstrip("/\\") + "/" + text
    )
    return relative_to(absolute, root)


def physical_path(root, relative, *, role="future_output", directory=False, expected_root_identity=None):
    """Resolve a declared path inside the physical root; never rebase a bad path.

    Revalidate before each use. This is not a sandbox or an atomic protection
    against an external process replacing a link after the check.
    """
    if safe_relative(relative) != relative or (relative == "." and not directory):
        raise ValueError("noncanonical_path")
    parts = PurePosixPath(relative).parts
    for part in parts:
        if (part.endswith((".", " ")) or re.search(r'[\x00-\x1f<>:"|?*]', part)
                or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)):
            raise ValueError("ambiguous_path")
    base = Path(root).resolve(strict=True)
    if not base.is_dir():
        raise ValueError("root_not_directory")
    if expected_root_identity is not None:
        stat = base.stat()
        identity = {"resolved": norm_path(base), "device": stat.st_dev, "inode": stat.st_ino}
        if identity != expected_root_identity:
            raise ValueError("root_identity_changed")
    target = Path(root) / relative
    # A dangling link is not an absent future output.
    current = Path(root)
    for part in parts:
        current = current / part
        if current.is_symlink():
            current.resolve(strict=True)
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise ValueError("physical_path_escape")
    if target.exists():
        if not (resolved.is_dir() if directory else resolved.is_file()):
            raise ValueError("path_type_mismatch")
    elif role == "existing_input" or directory:
        raise ValueError("input_missing")
    else:
        ancestor = resolved.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not ancestor.resolve(strict=True).is_relative_to(base):
            raise ValueError("output_parent_invalid")
    return resolved


def path_checks(contract):
    """Collect all explicit path failures for prepared contracts only."""
    if "path_roles" not in contract:
        return []
    declarations = [(p, role, False) for p, role in contract["path_roles"].items()]
    declarations += [(c.get("cwd") or ".", "existing_input", True) for c in contract.get("checks", [])]
    declarations += [(p, "existing_input", False) for c in contract.get("checks", []) for p in c["inputs"]]
    results = []
    for relative, role, directory in declarations:
        item = {"path": relative, "role": role, "directory": directory, "status": "pass"}
        try:
            resolved = physical_path(contract["workspace_root"], relative, role=role, directory=directory, expected_root_identity=contract.get("root_identity"))
            item["resolved_path"] = str(resolved)
            item["exists"] = resolved.exists()
            if resolved.exists() and resolved.is_file():
                stat = resolved.stat()
                item["identity"] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        except (OSError, ValueError, RuntimeError) as error:
            item.update(status="fail", reason=str(error) if isinstance(error, ValueError) else "path_unavailable")
        results.append(item)
    return results


def artifact_authorized(target, cwd, root, allowed_artifacts):
    """A write target is inside the workspace and exactly listed by the contract."""
    relative = _artifact_relative(target, cwd, root)
    if not relative or relative == ".":
        return False
    normalized = norm_path(relative)
    return any(normalized == norm_path(item) for item in allowed_artifacts)


def evaluate(contract, tool_name, tool_input, cwd, *, prompt="", input_digests=None):
    """Return (decision, reason, detail). decision: allow | deny | not_covered.

    The function never rewrites a call. `input_digests` maps registered input
    paths to current sha256 values computed by the caller; a missing digest is
    treated as unknown, which never yields a "no retest needed" decision.
    """
    tool = str(tool_name or "").casefold()
    if not contract:
        return "allow", None, {"rule": "contract_not_enforced"}
    mode = contract.get("execution_mode", "scoped_write")
    if mode == "disabled":
        if tool in FILE_TOOLS or tool in SHELL_TOOLS:
            return "deny", ("当前任务合约已停用。重新授权前不得修改工作区文件或执行命令；"
                            "请在任务合约历史中恢复一个明确版本。"), {"rule": "contract_disabled"}
        return "not_covered", None, {"rule": "contract_disabled_tool_not_covered"}
    if not contract.get("enforce"):
        return "allow", None, {"rule": "contract_not_enforced"}
    root = contract["workspace_root"]
    carrier = contract.get("progress_carrier", "pm_checkpoint")
    allowed = contract.get("allowed_artifacts", [])
    if "path_roles" in contract and (tool in FILE_TOOLS or tool in SHELL_TOOLS):
        blockers = contract.get("preparation_blockers", [])
        if blockers or not contract.get("actor_eligible"):
            return "deny", "当前会话或准备条件不允许执行此步骤。", {"rule": "preparation_blocked", "blockers": blockers}
        failures = [p for p in path_checks(contract) if p["status"] == "fail"]
        if failures:
            return "deny", "任务路径预检未通过；不要修改文件。", {"rule": "preparation_paths_invalid", "paths": failures}
        step = next((s for s in contract["steps"] if s["step_id"] == contract.get("current_step")), None)
        allowed = [step["artifact"]] if step and step["artifact"] else []
    if tool in FILE_TOOLS:
        if mode == "read_only":
            return "deny", ("当前任务处于只读阶段：不得创建、修改、删除或移动任何工作区文件。"
                            "本阶段只读取任务检查点和现有文件。"), {"rule": "read_only_file_write"}
        targets = file_targets(tool_name, tool_input)
        if targets is None:
            return "not_covered", None, {"rule": "unparsed_file_tool"}
        for target in targets:
            absolute = target if re.match(r"^[A-Za-z]:[\\/]|^/", target) else (str(cwd or root).rstrip("/\\") + "/" + target)
            if is_progress_file(absolute, carrier, allowed):
                return "deny", ("当前任务约定：进度保存在 PM 任务检查点，未授权新建本地进度文件 "
                    + PurePosixPath(norm_path(target)).name + "。本阶段唯一授权交付：" + contract.get("deliverable", "")[:200]), {"rule": "unauthorized_progress_file", "target": target}
            if "path_roles" in contract:
                relative = _artifact_relative(target, cwd, root)
                try:
                    physical_path(root, relative, role=contract["path_roles"].get(relative, "future_output"), expected_root_identity=contract.get("root_identity"))
                except (OSError, ValueError, RuntimeError, TypeError):
                    return "deny", "目标路径不在可确认的工作区内。", {"rule": "physical_path_invalid"}
            if not artifact_authorized(target, cwd, root, allowed):
                relative = _artifact_relative(target, cwd, root)
                return "deny", ("当前任务只允许写入明确授权的产物。未授权目标："
                    + (relative or target) + "。本阶段允许产物："
                    + (", ".join(allowed[:8]) if allowed else "无")), {
                        "rule": "unauthorized_artifact", "target": target, "relative": relative}
        return "allow", None, {"rule": "authorized_file_targets_checked", "targets": targets}
    if tool in SHELL_TOOLS:
        if mode == "read_only":
            return "deny", ("当前任务处于只读阶段：不得执行 shell、终端、测试或任何命令。"
                            "本阶段只读取任务检查点和现有文件。"), {"rule": "read_only_shell"}
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, list):
            command = " ".join(map(str, command))
        if not isinstance(command, str):
            return "not_covered", None, {"rule": "unparsed_shell"}
        if "path_roles" in contract and not any(matches_check(command, c) for c in contract.get("checks", [])):
            return "deny", "准备任务只允许已登记的检查命令。", {"rule": "unregistered_prepared_command"}
        shell_writes = SHELL_WRITE.findall(command)
        for match in shell_writes:
            if is_progress_file(match, carrier, allowed):
                return "deny", "当前任务约定：进度保存在 PM 任务检查点，未授权通过命令另存本地进度文件。", {"rule": "unauthorized_progress_file_shell", "target": match}
            if not artifact_authorized(match, cwd, root, allowed):
                return "deny", ("当前任务只允许通过命令写入明确授权的产物。未授权目标："
                    + (_artifact_relative(match, cwd, root) or match)), {
                        "rule": "unauthorized_artifact_shell", "target": match}
        if SHELL_MUTATION.search(command) and not shell_writes:
            return "deny", ("当前任务无法确认该命令的写入目标；为避免越过授权产物范围，"
                            "请改用宿主的结构化文件工具写入唯一授权产物。"), {"rule": "unparsed_shell_write"}
        for check in contract.get("checks", []):
            if not matches_check(command, check):
                continue
            here = norm_path(cwd or root)
            if here != norm_path(check.get("cwd") or root):
                return "allow", None, {"rule": "registered_check_other_cwd", "check_id": check["check_id"]}
            trigger = retest_trigger(check, prompt, input_digests)
            if trigger:
                return "allow", None, {"rule": "retest_trigger", "check_id": check["check_id"], "trigger": trigger}
            return "deny", ("已登记检查 " + check["check_id"] + " 已有观测证据（退出码 " + str(check["evidence"]["exit_code"])
                + "，" + check["evidence"]["observed_at"] + "），相关输入未变化且本轮未要求重测，不重复执行。"
                + "本阶段唯一授权交付：" + contract.get("deliverable", "")[:200]), {"rule": "duplicate_observed_check", "check_id": check["check_id"]}
        return "not_covered", None, {"rule": "unregistered_shell_command"}
    return "not_covered", None, {"rule": "tool_not_covered"}


def retest_trigger(check, prompt, input_digests):
    evidence = check.get("evidence")
    if not evidence:
        return "no_observed_evidence"
    if evidence.get("exit_code") != 0:
        return "previous_check_failed"
    if prompt and RETEST.search(prompt):
        return "user_requested_retest"
    recorded = evidence.get("input_digests") or {}
    if not recorded:
        return "inputs_unknown"
    if input_digests is None:
        return "inputs_unknown"
    for path, value in recorded.items():
        current = input_digests.get(path)
        if current is None:
            return "inputs_unknown"
        if current != value:
            return "inputs_changed"
    return None


def digest_inputs(root, paths, limit=8 * 1024 * 1024, expected_root_identity=None):
    """sha256 of registered relative inputs; missing/oversize files are omitted."""
    result = {}
    for rel in paths:
        safe = safe_relative(rel)
        if not safe:
            continue
        try:
            path = physical_path(root, safe, role="existing_input", expected_root_identity=expected_root_identity)
            if path.stat().st_size <= limit:
                result[safe] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError, RuntimeError):
            continue
    return result
