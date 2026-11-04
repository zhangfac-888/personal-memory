"""Source-grounded task intent; memory validity and execution are independent.
Closed grammars deliberately leave ambiguous, quoted and hypothetical text alone.
"""
from __future__ import annotations
import re

STATES=("planned","in_progress","blocked","postponed","completed","cancelled")
PENDING=("planned","in_progress","blocked","postponed")
COLUMNS=("record_kind","plan_status","plan_key")
_LABELS={"planned":"待做","in_progress":"进行中","blocked":"受阻","postponed":"延期","completed":"来源报告已完成","cancelled":"已取消"}
_HEDGE=re.compile(r"也许|或许|可能|考虑|假如|假设|如果|只有|除非|还没决定|据说|声称|是否|是不是|[?？]|\b(?:maybe|might|if|considering)\b",re.I)
_DATE=r"(?:(?:在|于)?(?:今天|明天|后天|下周[一二三四五六日天]?|本周[一二三四五六日天]?|这周|下个月|本月|月底|下月|稍后|之后|随后|周[一二三四五六日天]|\d{4}[-/]\d{1,2}[-/]\d{1,2})[，,\s]*)?"
_INTRO=re.compile(r"^(?:请)?记住(?:一条|以下)?(?:(?:仅限|仅在)?(?:当前|这个|本)项目的)?(?:合成测试)?(?:计划|记录)[：:]\s*")

# Only an explicit synthetic/no-execution preface is transparent to this
# bounded grammar. Preserve it in quote/source; do not strip arbitrary
# material, quoted examples, third-party claims or conditions.
_SYNTHETIC_INTRO=re.compile(r"^(?:以下|下面)(?:是|为)(?:一条)?合成测试(?:资料|记录|计划)[，,]\s*(?:不需要|无需)(?:实际)?执行[：:]\s*")

def classify(quote):
    if not isinstance(quote,str) or len(quote)>1000:return None
    original=quote.strip()
    # A presentation row is not an independently asserted user plan. Preserve
    # uncertain/reference material for explicit review rather than invent task
    # names containing table separators or numbering.
    if re.search(r"\||^(?:[-*+]\s+|\d+[.)、]\s*)",original):return None
    value=_INTRO.sub("",original,count=1)
    value=_SYNTHETIC_INTRO.sub("",value,count=1).strip().rstrip("。")
    if re.search(r"所有(?:回答|回复)|每次(?:回答|回复)|(?:以后|从现在).{0,30}(?:回答|回复)",value):return None
    if _HEDGE.search(value) or re.search(r"\x60|[\n；;]|^>|<(?:document|material|untrusted_text|log|source)\b",value,re.I):return None
    restart=bool(re.search(r"重新(?:计划|准备|安排)|更正[：:]",value))
    value=re.sub(r"^更正[：:]\s*","",value)
    state=None;task=None
    future=re.fullmatch(r"(?:(?:我|我们|本项目|当前项目|这个项目)[，,\s]*)?(?:(?:已(?:经)?)?(?:决定|确定)(?:要|将)|(?:决定|确定)(?=(?:在|于)?(?:今天|明天|下周|本周|下个月|下月|月底))|(?:重新)?(?:计划|准备|打算|安排))[，,：:\s]*"+_DATE+r"(?P<task>[^，。！？：:]{2,70})[。]?",value)
    step=re.fullmatch(r"(?:下一步|下一项)(?:工作|任务)?(?:已确定为|确定为|是|为|[：:])[，,\s]*"+_DATE+r"(?P<task>[^，。！？：:]{2,70})[。]?",value)
    match=future or step
    if match:task=match["task"];state="planned"
    if state is None:
        cancel=re.fullmatch(r"(?:我(?:们)?(?:已(?:经)?)?)?取消(?:了)?(?P<task>[^，。！？：:]{2,70})[。]?",value)
        if cancel:task=cancel["task"];state="cancelled"
    if state is None:
        patterns=(
            ("in_progress",r"(?:已(?:经)?开始|正在进行|进行中|部分完成|仅完成)[^。！？]*"),
            ("postponed",r"(?:已(?:经)?)?(?:延期|推迟)[^。！？]*"),
            ("blocked",r"(?:已(?:经)?)?(?:受阻|暂停)[^。！？]*"),
            ("cancelled",r"(?:已(?:经)?取消|不再执行)[^。！？]*"),
            ("completed",r"已(?:经)?完成[。]?"),
        )
        for status,tail in patterns:
            match=re.fullmatch(r"(?P<task>[^，。！？：:]{2,70}?)"+tail,value)
            if match:task=match["task"];state=status;break
    if state is None:return None
    # A later clause may contradict the first state. Keep that full assertion
    # for review rather than silently presenting a possibly cancelled task.
    signals=set()
    if re.search(r"已(?:经)?开始|正在进行|进行中|部分完成|仅完成",value):signals.add("in_progress")
    if re.search(r"延期|推迟",value):signals.add("postponed")
    if re.search(r"受阻|暂停",value):signals.add("blocked")
    if re.search(r"已(?:经)?取消|不再执行|^(?:我(?:们)?(?:已(?:经)?)?)?取消",value):signals.add("cancelled")
    if re.search(r"已(?:经)?完成",value) and not re.search(r"尚未|还未|未完成",value):signals.add("completed")
    if state!="planned" and len(signals)>1:return None
    task=task.strip().rstrip("。")
    # Retain the whole assertion; never accept a negated or conditional task
    # as an affirmative completion merely by moving words into the task name.
    if not 2<=len(task)<=70 or re.search(r"尚未|还未|没有|并未|不要|不应|不能|不确定|请问|[，,。！？：:]|(?:未|不)$",task):return None
    if state=="completed" and re.search(r"尚未|还未|未完成|没有|并未",value):return None
    key=re.sub(r"\s+"," ",task).casefold()
    if state=="planned" and key.startswith("完成") and len(key)>3:key=key[2:]
    return {"task":task,"key":key,"status":state,"restart":restart,"quote":original}

def columns(content,source_quote):
    parsed=classify(source_quote) if content==source_quote else None
    return {"record_kind":"plan","plan_status":parsed["status"],"plan_key":parsed["key"]} if parsed else {"record_kind":"memory","plan_status":None,"plan_key":None}

def can_replace(before,after):
    if before in {"completed","cancelled"} and after["status"]!=before:return after["restart"]
    return True

def query_states(query):
    if re.search(r"取消.{0,5}(?:计划|任务)|(?:计划|任务).{0,5}取消",query):return ("cancelled",)
    if re.search(r"已完成.{0,5}(?:计划|任务|事项)|完成了什么|完成了哪些|(?:计划|任务).{0,5}已完成",query):return ("completed",)
    if re.search(r"下一步|下一项|接下来|待办|未完成.{0,5}(?:计划|任务)|(?:计划|任务).{0,5}未完成",query):return PENDING
    return None

def filter_gate(alias,states):
    if not states:return "1",[]
    return "("+alias+".record_kind!='plan' OR "+alias+".plan_status IN ("+",".join("?" for _ in states)+"))",list(states)

def context_note(items):
    if not any(item.get("record_kind")=="plan" for item in items):return ""
    return "计划记录的status仅表示记忆是否有效，plan_status才是任务执行状态；planned为待做，in_progress为进行中，blocked为受阻，postponed为延期，cancelled为取消，均不能写成已完成。completed只是来源报告完成，不表示独立验收。请保留原文的范围、时间与未完成部分。\n"


def real_pending_query(query):
    # Historical/cancelled retrieval remains available; explicitly named test
    # records remain testable. Current real work must not inherit synthetic
    # progress or raw historical conversation as authoritative task state.
    return query_states(query)==PENDING and not re.search(r"合成测试|测试记录|测试资料",query)

def source_allowed_for_query(query,kind,qualifiers):
    return not (real_pending_query(query) and qualifiers.get('synthetic')
                and kind not in {'Rule','Preference'})
