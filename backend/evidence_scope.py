"""Make read boundaries explicit without converting missing evidence to negatives."""
from copy import deepcopy

ANSWER_EVIDENCE_NOTE=("回答只写与问题直接相关且可逐项引用原文或明确元数据支持的结论；每项事实应对应其证据，无法对应则省略或说未知。不要为凑解释加入推测。"
    "回答事实须以来源正文、逐条来源限定和本次明确返回的证据为依据。"
    "未请求修订历史、未返回历史或只读某条记忆的版本记录时，不能说业务计划没有后续修订、没有其他进度或后续安排；应说本次读取未提供这方面证据。"
    "即使本条记忆的版本记录全部返回，也不等于已遍历同一业务计划的其他记录。"
    "存储时间、版本号、内部账户、项目名和来源标题是建档信息，不证明业务发生时间、真实或测试性质、是否执行或实际完成。"
    "计划名称是正文中的业务对象，不要混称为来源标题。句式、标点和用词是否相似，不支持判断原始段落、文件或聊天是否相同，也不要给这类猜测。只有来源正文明确标注才称合成测试。"
    "Reported仅表示据来源报告，不等于用户口述；assertion_origin未确认用户来源时，不得称用户说过或口述。来源明确报告的成果应保留并说明据来源报告；未独立核验不等于没有完成。推测如非用户所问且无依据，应省略。\n")

def detail_scope(source_offset,source_length,history_requested,returned_rows=0,history_truncated=False):
    return {"source_record":{"offset":source_offset,"fully_returned":source_offset==0 and source_length<=8000},
            "history":{"requested":bool(history_requested),"returned_rows":returned_rows if history_requested else None,
                       "row_limit":50,"truncated":bool(history_truncated) if history_requested else None,
                       "scope":"this_memory_record_only","other_plan_records_enumerated":False},
            "business_chronology_from_storage_timestamps":False}

def conservative_scope(payload,history_requested=False):
    if isinstance(payload.get("evidence_scope"),dict):return deepcopy(payload["evidence_scope"])
    revisions=payload.get("revisions")
    return {"source_record":{"offset":None,"fully_returned":None},
            "history":{"requested":bool(history_requested),"returned_rows":len(revisions) if isinstance(revisions,list) else None,
                       "row_limit":None,"truncated":None,"scope":"this_memory_record_only",
                       "other_plan_records_enumerated":False},"business_chronology_from_storage_timestamps":False}

def without_audit_accounts(payload):
    result=deepcopy(payload);memory=result.get("memory",{})
    for key in ("created_by","last_modified_by"):memory.pop(key,None)
    for row in result.get("revisions",[]):
        row.pop("actor_id",None);row.pop("before_snapshot",None);row.pop("after_snapshot",None)
    return result

def model_detail(payload):
    """Project only at the model boundary; internal service dates stay compatible."""
    result=without_audit_accounts(payload);memory=result.get("memory",{});source=result.get("source",{})
    storage=dict(result.get("storage_metadata",{}))
    for key in ("created_at","updated_at"):
        if key in memory:storage["memory_"+key]=memory.pop(key)
    if "created_at" in source:storage["source_created_at"]=source.pop("created_at")
    if storage:
        storage["meaning"]="library_storage_metadata_not_business_event_time"
        result["storage_metadata"]=storage
    return result


def answer_evidence(payload):
    origin=payload.get("assertion_origin") or {"reporter":"unspecified","basis":"source_text_only"}
    facts={"report_origin":origin,"history_scope":"this_memory_record_only_not_all_business_updates",
           "facts_must_reference":"source_text_or_explicit_returned_metadata"}
    if payload.get("source_qualifiers",{}).get("no_execution"):
        facts["nonexecution_instruction"]={"kind":"instruction_only","actual_execution_outcome":"not_established_by_this_instruction",
            "answer_boundary":"来源要求不实际执行；实际是否执行未知。不得改写为已核实未执行或不涉及实际执行。"}
    return facts
