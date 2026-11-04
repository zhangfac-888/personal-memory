"""Source-record identity, deliberately separate from original-document identity."""
from __future__ import annotations

def record_attribution(memory, source=None):
    mid=memory.get("memory_id");sid=memory.get("source_id")
    valid=isinstance(sid,str) and bool(sid.strip())
    state="search_reference" if valid else "missing"
    if source is not None:
        other=source.get("source_id")
        state="matching_detail" if valid and other==sid else "mismatch" if valid or other else "missing"
    return {"memory_id":mid,"source_id":sid if state in {"search_reference","matching_detail"} else None,
            "kind":"memory_source_record","reference_status":state,
            "original_document_relationship":"not_established_by_source_ids"}

def source_identity_summary(items):
    groups={};unknown=0
    for item in items:
        ref=item.get("source_attribution") or record_attribution(item)
        sid=ref.get("source_id")
        if ref.get("reference_status") not in {"search_reference","matching_detail"} or not isinstance(sid,str) or not sid.strip():
            unknown+=1;continue
        mids=groups.setdefault(sid,[]);mid=ref.get("memory_id")
        if isinstance(mid,str) and mid not in mids:mids.append(mid)
    return {"scope":"returned_items_only","distinct_source_record_count":len(groups),
            "unresolved_reference_count":unknown,
            "groups":[{"source_id":sid,"memory_ids":mids} for sid,mids in groups.items()],
            "title_is_identity":False,"original_document_relationship":"not_established_by_source_ids"}

def source_identity_note(summary):
    n=summary["distinct_source_record_count"];unknown=summary["unresolved_reference_count"]
    prefix=(f"本轮返回项中，有有效来源引用的条目对应{n}条"+("不同的" if n>1 else "")+"库内来源记录。") if n else "本轮返回项尚无可确认的库内来源引用。"
    if unknown:prefix+=f"另有{unknown}个返回项的来源引用未确认，不计入上述数量。"
    return prefix+"来源标题和客户端名称只是显示信息，不能用于合并来源记录。来源记录编号只标识库内记录：不同编号不能证明原始文件、原始消息或聊天不同；相同编号也不证明整份文件或会话已读取。原始出处关系没有明确证据时应说无法确认。来源数量只针对本轮返回项，不代表全库。\n"
