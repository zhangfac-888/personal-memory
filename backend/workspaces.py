"""Deterministic workspace identity shared by automatic read and write paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .service import Problem, canonical, now, uid

GLOBAL_PROJECT = "global"
PERSONAL_PROJECT = "personal-sessions"
PERSONAL_NAME = "个人会话"


def normalized_path(value: str) -> str:
    return str(Path(value).expanduser().resolve()).replace("\\", "/").rstrip("/").casefold()


def in_roots(path: str, roots: list[str]) -> bool:
    current = normalized_path(path)
    return any(current == root or current.startswith(root + "/") for root in roots)


def _git_identity(cwd: Path) -> tuple[Path, Path] | None:
    """Return (worktree root, common git directory) without reading source files."""
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            common = marker.resolve()
            if marker.is_file():
                try:
                    line = marker.read_text("utf8").strip()
                    if line.lower().startswith("gitdir:"):
                        common = (candidate / line.split(":", 1)[1].strip()).resolve()
                        common_file = common / "commondir"
                        if common_file.is_file():
                            common = (common / common_file.read_text("utf8").strip()).resolve()
                        elif common.parent.name == "worktrees":
                            common = common.parent.parent
                except (OSError, UnicodeError):
                    pass
            return candidate, common
    return None


def _git_common_at(cwd: Path) -> Path | None:
    """Find the Git common directory containing cwd, including linked worktrees."""
    for candidate in (cwd.resolve(), *cwd.resolve().parents):
        marker = candidate / ".git"
        if marker.is_file():
            try:
                line = marker.read_text("utf8").strip()
                if line.lower().startswith("gitdir:"):
                    common = (candidate / line.split(":", 1)[1].strip()).resolve()
                    if (common / "commondir").is_file():
                        return (common / (common / "commondir").read_text("utf8").strip()).resolve()
                    return common.parent.parent if common.parent.name == "worktrees" else common
            except (OSError, UnicodeError):
                pass
        elif marker.is_dir():
            return marker.resolve()
    return None


def workspace_identity(cwd: str | None) -> tuple[str, str, str]:
    """Return stable key, display name, resolution without scanning file content."""
    if not isinstance(cwd, str) or not cwd.strip():
        return "personal:", PERSONAL_NAME, "personal"
    raw = Path(cwd).expanduser()
    try:
        resolved = raw.resolve()
    except (OSError, RuntimeError):
        raise Problem("VALIDATION_ERROR", "工作目录无效", 422)
    # Desktop tools may open at the profile directory. It is not a project.
    if resolved == Path.home().resolve():
        return "personal:", PERSONAL_NAME, "personal"
    git_identity = _git_identity(resolved)
    if git_identity:
        root, common = git_identity
        key = hashlib.sha256(("git:" + normalized_path(str(common))).encode("utf8")).hexdigest()
        return key, root.name or str(root), "git-root"
    key = hashlib.sha256(("directory:" + normalized_path(str(resolved))).encode("utf8")).hexdigest()
    return key, resolved.name or str(resolved), "directory"


def _unique_project_name(conn, base: str, root_key: str) -> str:
    name = base.strip()[:90] or "自动项目"
    if not conn.execute("SELECT 1 FROM projects WHERE lower(name)=lower(?) AND status='active'", (name,)).fetchone():
        return name
    return f"{name} · {root_key[:8]}"


def resolve_scope(conn, client: dict, cwd: str | None, *, create: bool) -> dict:
    client = current_client(conn, client)
    if client.get("revoked"):
        raise Problem("UNAUTHENTICATED", "自动记忆凭据无效", 401)
    if not client.get("enabled", 1):
        raise Problem("AUTOMATION_PAUSED", "自动记忆已暂停", 409)
    if client.get("access_mode", "bound") != "library":
        raise Problem("NOT_FOUND", "此接口仅用于库级自动记忆", 404)
    excluded = effective_exclusions(conn, client)
    if cwd and in_roots(cwd, excluded):
        raise Problem("FORBIDDEN", "此工作区已从自动记忆排除", 403)
    mapping = conn.execute("SELECT * FROM workspace_directory_mappings WHERE directory=? AND active=1",
        (normalized_path(cwd),)).fetchone() if cwd else None
    if mapping:
        root = mapping["workspace_root"]
        if in_roots(root, excluded) or mapping["project_id"] in excluded_projects(conn, client):
            raise Problem("FORBIDDEN", "映射项目或来源工作区已被排除", 403)
        # Revalidate filesystem identity on each use; a later junction/Git
        # change must not silently widen an administrator's exact mapping.
        _validate_mapping_paths(cwd, root)
        key, _, _ = workspace_identity(root)
        alias = conn.execute("SELECT project_id FROM workspace_aliases WHERE root_key=?", (key,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE project_id=? AND status='active'", (mapping["project_id"],)).fetchone()
        if not project or not alias or alias["project_id"] != mapping["project_id"]:
            raise Problem("WORKSPACE_MAPPING_CONFLICT", "明确目录映射的原项目已改变，请复核映射", 409)
        return {"project_id":project["project_id"], "project_name":project["name"],
            "root_key":key, "resolution":"explicit-directory-mapping", "created":False,
            "workspace_root":root, "mapping_id":mapping["mapping_id"]}
    root_key, name, resolution = workspace_identity(cwd)
    alias = conn.execute("SELECT * FROM workspace_aliases WHERE root_key=?", (root_key,)).fetchone()
    if alias:
        project = conn.execute("SELECT * FROM projects WHERE project_id=?", (alias["project_id"],)).fetchone()
        if not project or project["status"] != "active":
            raise Problem("NOT_FOUND", "自动项目不可用", 404)
        conn.execute("UPDATE workspace_aliases SET last_seen_at=? WHERE alias_id=?", (now(), alias["alias_id"]))
        register_roots(conn, project["project_id"], cwd, resolution)
        if project["project_id"] in excluded_projects(conn, client):
            raise Problem("FORBIDDEN", "此项目的来源工作区已被排除", 403)
        return {"project_id": project["project_id"], "project_name": project["name"],
                "root_key": root_key, "resolution": resolution, "created": False}
    if resolution == "personal":
        project_id = PERSONAL_PROJECT
    else:
        project_id = uid()
        stamp = now()
        conn.execute("INSERT INTO projects(project_id,name,description,created_at,updated_at) VALUES (?,?,?,?,?)",
            (project_id, _unique_project_name(conn, name, root_key), "由已授权 AI 工具自动识别", stamp, stamp))
    conn.execute("INSERT INTO workspace_aliases(alias_id,client_id,root_key,project_id,resolution,created_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
        (uid(), None, root_key, project_id, resolution, now(), now()))
    project = conn.execute("SELECT name FROM projects WHERE project_id=?", (project_id,)).fetchone()
    register_roots(conn, project_id, cwd, resolution)
    return {"project_id": project_id, "project_name": project["name"], "root_key": root_key,
            "resolution": resolution, "created": True}


def excluded_projects(conn, client: dict) -> set[str]:
    """Resolve directory exclusions to projects using the same identity rules."""
    result = set()
    roots = effective_exclusions(conn, client)
    # Keep excluded mapped sources excluded even after routing is revoked;
    # revocation does not delete the captures made under that mapping.
    result.update(row["project_id"] for row in conn.execute("SELECT directory,project_id FROM workspace_directory_mappings")
        if in_roots(row["directory"], roots))
    result.update(r[0] for r in conn.execute("SELECT DISTINCT wr.project_id FROM workspace_roots wr,json_each(?) x WHERE wr.root=x.value OR substr(wr.root,1,length(x.value)+1)=x.value||'/'", (canonical(roots),)))
    for value in roots:
        try:
            root_key, _, _ = workspace_identity(value)
        except (OSError, RuntimeError, Problem):
            continue
        row = conn.execute("SELECT project_id FROM workspace_aliases WHERE root_key=?", (root_key,)).fetchone()
        if row:
            result.add(row["project_id"])
    return result


def _validate_mapping_paths(directory, workspace_root):
    for value in (directory, workspace_root):
        if not isinstance(value,str) or not value.strip() or not Path(value).is_absolute() or not Path(value).is_dir():
            raise Problem("VALIDATION_ERROR", "目录映射需要存在的绝对目录", 422)
    current, root = Path(directory).resolve(), Path(workspace_root).resolve()
    if root == Path.home().resolve() or root.parent == root or current == root or not current.is_relative_to(root):
        raise Problem("VALIDATION_ERROR", "只允许明确项目内的子目录映射，不能映射个人根或外部目录", 422)
    identity = _git_identity(current)
    if identity and identity[0].is_relative_to(root) and identity[0] != root:
        raise Problem("WORKSPACE_MAPPING_CONFLICT", "子目录有独立 Git 项目，不能并入父项目", 409)


def list_directory_mappings(service, admin):
    if admin.get("kind") != "admin":
        raise Problem("FORBIDDEN", "仅本机管理员可查看目录映射", 403)
    with service.db.read() as conn:
        return {"items":[dict(r) for r in conn.execute("SELECT * FROM workspace_directory_mappings ORDER BY directory")],
            "matching":"exact_directory_only", "history_rewritten":False}


def add_directory_mapping(service, admin, directory, workspace_root, project_id):
    if admin.get("kind") != "admin" or not admin.get("can_write"):
        raise Problem("FORBIDDEN", "仅本机管理员可设置目录映射", 403)
    _validate_mapping_paths(directory, workspace_root)
    directory, workspace_root = normalized_path(directory), normalized_path(workspace_root)
    key, _, _ = workspace_identity(workspace_root)
    with service.db.tx() as conn:
        service._project(conn, project_id, True)
        alias = conn.execute("SELECT project_id FROM workspace_aliases WHERE root_key=?", (key,)).fetchone()
        if project_id in {GLOBAL_PROJECT,PERSONAL_PROJECT} or not alias or alias["project_id"] != project_id:
            raise Problem("WORKSPACE_MAPPING_CONFLICT", "项目根目录必须对应指定的已有项目", 409)
        old = conn.execute("SELECT * FROM workspace_directory_mappings WHERE directory=?", (directory,)).fetchone()
        if old:
            if old["workspace_root"] != workspace_root or old["project_id"] != project_id:
                raise Problem("WORKSPACE_MAPPING_CONFLICT", "此目录已有不同映射，请先复核", 409)
            return dict(old)  # An add must not silently revive a revoked mapping.
        stamp=now();mapping_id=uid()
        conn.execute("INSERT INTO workspace_directory_mappings VALUES (?,?,?,?,1,1,?,?)",
            (mapping_id,directory,workspace_root,project_id,stamp,stamp))
        service._log(conn,admin,"workspace_directory_mapping_add",mapping_id)
        return dict(conn.execute("SELECT * FROM workspace_directory_mappings WHERE mapping_id=?",(mapping_id,)).fetchone())


def set_directory_mapping_active(service, admin, mapping_id, active, expected_version):
    if admin.get("kind") != "admin" or not admin.get("can_write"):
        raise Problem("FORBIDDEN", "仅本机管理员可撤销或恢复映射", 403)
    if type(active) is not bool or type(expected_version) is not int:
        raise Problem("VALIDATION_ERROR", "映射状态或版本无效", 422)
    with service.db.tx() as conn:
        row=conn.execute("SELECT * FROM workspace_directory_mappings WHERE mapping_id=?",(mapping_id,)).fetchone()
        if not row:raise Problem("NOT_FOUND","目录映射不存在",404)
        if row["version"] != expected_version:raise Problem("VERSION_CONFLICT","映射已经改变，请重新读取",409)
        if row["active"] == int(active):return dict(row)
        if active:_validate_mapping_paths(row["directory"],row["workspace_root"])
        conn.execute("UPDATE workspace_directory_mappings SET active=?,version=version+1,updated_at=? WHERE mapping_id=?",
            (int(active),now(),mapping_id))
        service._log(conn,admin,"workspace_directory_mapping_enable" if active else "workspace_directory_mapping_revoke",mapping_id)
        return dict(conn.execute("SELECT * FROM workspace_directory_mappings WHERE mapping_id=?",(mapping_id,)).fetchone())


def current_client(conn, client):
    row = conn.execute("SELECT * FROM automation_clients WHERE client_id=?", (client["client_id"],)).fetchone()
    if not row or row["revoked"]:
        raise Problem("UNAUTHENTICATED", "自动记忆凭据无效", 401)
    if not row["enabled"]:
        raise Problem("AUTOMATION_PAUSED", "自动记忆已暂停", 409)
    return dict(row)


def effective_exclusions(conn, client):
    # A host's two credentials share the union of their restrictions. Editing
    # one credential never clears the other one's independently configured rule.
    values = json.loads(client.get("excluded_roots_json") or "[]")
    if client.get("app") != "test":
        for row in conn.execute("SELECT excluded_roots_json FROM automation_clients WHERE app=? AND access_mode='library' AND revoked=0", (client["app"],)):
            values.extend(json.loads(row[0]))
    return sorted(set(values))


def register_roots(conn, project_id, cwd, basis):
    if not cwd or basis == "personal":
        return
    actual = Path(cwd).resolve()
    roots = [actual]
    identity = _git_identity(actual)
    if identity:
        root, common = identity
        roots.append(root)
        if common.name == ".git":
            roots.append(common.parent)
    for root in roots:
        conn.execute("INSERT INTO workspace_roots VALUES (?,?,?,?) ON CONFLICT(project_id,root) DO UPDATE SET observed_at=excluded.observed_at",
            (project_id, normalized_path(str(root)), basis, now()))


def library_actor(client: dict, current_project_id: str | None, excluded: set[str] | None = None) -> dict:
    """Library access is explicit and excludes a system-disabled client."""
    return {"kind": "client", "id": client["client_id"], "projects": None,
            "current_project_id": current_project_id, "can_write": False, "library": True,
            "registered_library": True,
            "excluded_projects": sorted(excluded or ())}


def scope_payload(scope: dict) -> str:
    return canonical(scope)
