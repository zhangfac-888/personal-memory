"""Deterministic opt-in automation policy and reversible, source-grounded review."""
import json,re
from .service import Problem,canonical,digest,now,uid
from .workspaces import GLOBAL_PROJECT

POLICY="explicit-low-risk-v1"
LABELS={"structured_fragment":"结构化碎片","authorization":"授权或凭据信息","untraceable":"非明确原话或执行观察",
 "global_long_term":"全局长期要求","existing_plan_change":"覆盖、取消或替换已有计划","unlinked_new_plan":"新计划无稳定任务关联",
 "multiple_plans":"一句或同一来源包含多个计划","conflict":"与有效记忆冲突","uncertain":"含糊或尚不满足生效条件",
 "duplicate":"已有相同内容","low_risk_explicit":"明确来源的低风险内容","revoked_content":"已撤回的相同内容"}


def suppression_key(text):return digest("normalized:"+"".join(text.split()))


def invalid_content(text):
    if re.search(r'(?i)\b(?:bearer\s+|sk-[a-z0-9]{8}|api[_ -]?key\s*[:=]|token\s*[:=]|password\s*[:=])',text):return "authorization"
    if re.match(r'^(?:我)?(?:允许|同意|批准|授权)(?:本次|这次|此次|本轮|你|将|发送|访问|读取|执行|操作)',text.strip()):return "authorization"
    if re.match(r'^\s*[\[{]',text) or re.search(r'"(?:answer|questionItemId|question|role|content)"\s*:',text):return "structured_fragment"
    if re.search(r'<(?:send_user_message|tool_result|function_call)',text) or re.fullmatch(r'[\W\d_]+',text):return "structured_fragment"
    return None


def linked_task(conn,payload,client_id,project_id=None):
    task_id=payload.get("task_id")
    project_id=payload.get("scope",{}).get("project_id",project_id)
    if not project_id:return None
    row=conn.execute("SELECT t.*,ts.session_id member_session FROM task_checkpoint_sessions ts JOIN task_checkpoints t USING(task_id) WHERE ts.client_id=? AND ts.session_id=? AND ts.project_id=?",(client_id,payload["session_id"],project_id)).fetchone()
    if not row:return None
    if task_id and task_id==row["task_id"]:return task_id
    # A receiver already attached to a different origin is a proven association.
    if (row["origin_client_id"],row["origin_session_id"])!=(client_id,payload["session_id"]):return row["task_id"]
    return None


def decide(conn,item,source,payload,client_id,project_id,conflict=False):
    quote=item["quote"];bad=invalid_content(quote)
    if bad:return {"outcome":"skipped","reasons":[bad],"task_id":None}
    task=linked_task(conn,payload,client_id,project_id)
    if source["role"] not in {"user","tool"} or quote not in source["text"]:
        return {"outcome":"skipped","reasons":["untraceable"],"task_id":task}
    reasons=[]
    if source["role"]=="tool" and (not task or not source.get("tool_observation")):reasons.append("untraceable")
    text=source["text"]
    if project_id==GLOBAL_PROJECT or (item["type"] in {"Rule","Preference"} and re.search(r"所有项目|全部项目|跨项目|全局|所有任务|全部任务|每个任务|每次回答|每次回复|每次输出|所有回答|所有回复|与我交流|以后都",quote) and not re.search(r"当前项目|本项目|这个项目",quote)):reasons.append("global_long_term")
    plan=item.get("plan")
    if plan:
        if not task:reasons.append("unlinked_new_plan")
        existing=conn.execute("SELECT 1 FROM memories WHERE project_id=? AND record_kind='plan' AND plan_key=? AND status='Active'",(project_id,plan["key"])).fetchone()
        if existing or plan["status"]=="cancelled" or item.get("replacement"):reasons.append("existing_plan_change")
    if re.search(r"(?:取消|替换|覆盖|取代|作废).{0,40}(?:计划|任务)|(?:计划|任务).{0,40}(?:取消|替换|覆盖|取代|作废)",quote) and "existing_plan_change" not in reasons:reasons.append("existing_plan_change")
    if plan and re.search(r"并且|然后|同时|以及|和|及|与|、|；|;",plan["task"]):reasons.append("multiple_plans")
    if len(re.findall(r"计划|打算|准备|下一步",text))>1 or re.search(r"(?:计划|打算|准备)[^。！？\n]{1,120}[；;、][^。！？\n]{1,120}",text):reasons.append("multiple_plans")
    if item.get("replacement") and "existing_plan_change" not in reasons:reasons.append("conflict")
    if conflict:reasons.append("conflict")
    if not item.get("active"):reasons.append("uncertain")
    return {"outcome":"review" if reasons else "active","reasons":list(dict.fromkeys(reasons)) or ["low_risk_explicit"],"task_id":task}


def record(conn,event_id,memory_id,project_id,source_role,quote,decision):
    conn.execute("INSERT INTO automatic_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",(uid(),event_id,memory_id,project_id,decision.get("task_id"),source_role,digest(quote),decision["outcome"],canonical(decision["reasons"]),now()))


def _admin(admin):
    if admin.get("kind")!="admin":raise Problem("FORBIDDEN","需要管理权限",403)


def recent(service,admin,limit=100,project_id=None):
    _admin(admin)
    if type(limit) is not int or not 1<=limit<=100:raise Problem("VALIDATION_ERROR","数量须为1到100",422)
    with service.db.read() as conn:
        where="WHERE d.project_id=?" if project_id else "";args=[project_id] if project_id else []
        rows=conn.execute("SELECT d.*,m.content,m.source_quote,m.status,m.version,m.type,m.confidence,m.source_id,s.visibility,p.name project_name FROM automatic_decisions d LEFT JOIN memories m USING(memory_id) LEFT JOIN sources s USING(source_id) JOIN projects p ON p.project_id=d.project_id "+where+" ORDER BY d.created_at DESC,d.rowid DESC LIMIT ?",[*args,limit]).fetchall()
        items=[]
        for row in rows:
            value=dict(row);value["reasons"]=[LABELS.get(x,x) for x in json.loads(value.pop("reasons_json"))]
            value["can_undo"]=value["status"] in {"Active","Candidate"} and value["content"]==value["source_quote"]
            value["revision_count"]=conn.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?",(value["memory_id"],)).fetchone()[0] if value["memory_id"] else 0
            items.append(value)
    n=len(items);human=sum(x["outcome"]=="review" for x in items)
    return {"policy":POLICY,"items":items,"coverage":"most recent policy decisions only; not an entire-library inventory",
        "sample_count":n,"human_review_count":human,"human_review_per_100":round(human*100/n,1) if n else None,
        "counts":{k:sum(x["outcome"]==k for x in items) for k in ["active","review","skipped","suppressed"]}}


def undo(service,admin,memory_id,expected_version):
    _admin(admin)
    if type(expected_version) is not int:raise Problem("VALIDATION_ERROR","版本须为整数",422)
    with service.db.tx() as conn:
        row=conn.execute("SELECT m.*,l.evidence_hash FROM memories m JOIN automation_memory_links l USING(memory_id) WHERE m.memory_id=?",(memory_id,)).fetchone()
        if not row:raise Problem("NOT_FOUND","不是自动记忆",404)
        if row["version"]!=expected_version:raise Problem("VERSION_CONFLICT","记录已变化，请刷新后查看",409)
        if row["status"]=="Deleted":return {"memory_id":memory_id,"status":"Deleted","replayed":True}
        if row["status"] not in {"Active","Candidate"} or row["content"]!=row["source_quote"]:raise Problem("INVALID_TRANSITION","记录已被另行修改，请查看历史后处理",409)
        for key in [row["evidence_hash"],suppression_key(row["source_quote"])]:conn.execute("INSERT OR IGNORE INTO automation_suppressions VALUES (?,?,?)",(row["project_id"],key,now()))
        service._change_row(conn,dict(row),{"status":"Deleted","deleted_from_status":row["status"]},admin,"automatic_undo","用户一步撤回自动记录；来源与版本保留",uid())
    return {"memory_id":memory_id,"status":"Deleted","suppressed_exact_and_whitespace_normalized_content":True}


def markdown(service,admin,project_id=None,include_private=False):
    _admin(admin)
    if type(include_private) is not bool:raise Problem("VALIDATION_ERROR","私密来源开关无效",422)
    query="SELECT m.*,s.title,s.visibility,s.origin_app,s.external_ref,s.content source_content,p.name project_name FROM memories m JOIN sources s USING(source_id) JOIN projects p ON p.project_id=m.project_id WHERE 1=1";args=[]
    if project_id:query+=" AND m.project_id=?";args.append(project_id)
    if not include_private:query+=" AND s.visibility='agent_allowed'"
    with service.db.read() as conn:rows=conn.execute(query+" ORDER BY p.name,m.status,m.updated_at,m.memory_id",args).fetchall()
    lines=["# Personal Memory 可读导出","", "导出时间："+now(),"范围："+("包含本地私密来源（本地管理员明确选择）" if include_private else "仅允许 Agent 读取的来源；私密来源已排除"),"注意：状态和入库时间不证明业务已完成；下列仅为存储记录。",""]
    labels={"Active":"有效记忆","Candidate":"待审核候选","Superseded":"已替代历史","Archived":"已归档","Deleted":"已撤回/回收站"}
    for row in rows:
        from .automation_text import synthetic_qualifiers
        qualifiers=synthetic_qualifiers(row["source_content"],row["source_quote"])
        if qualifiers.get("synthetic"):
            lines.extend(["**来源明确标注：合成测试资料。**"+(" 原文要求不实际执行；实际是否执行需另看观察证据。" if qualifiers.get("no_execution") else "")])
        lines.extend(["## "+row["project_name"]+" · "+labels.get(row["status"],row["status"]),"记录："+row["memory_id"]+" · 版本 "+str(row["version"])+" · "+row["confidence"],"来源记录："+row["source_id"]+" · 可见性 "+row["visibility"],"类型："+row["type"]+"；计划状态："+str(row["plan_status"] or "不适用"),"建档更新时间："+row["updated_at"],"正文："])
        lines.extend("    "+line for line in row["content"].splitlines());lines.append("原文摘录：")
        lines.extend("    "+line for line in row["source_quote"].splitlines());lines.extend(["来源外部引用："+str(row["external_ref"] or "未提供"),""])
    return "\n".join(lines)+"\n"
