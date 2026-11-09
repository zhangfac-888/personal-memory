"""Small, dependency-free filters shared by hooks and the server."""
import re,json
from datetime import datetime, timedelta, timezone
from .source_identity import source_identity_summary, source_identity_note
from .evidence_scope import ANSWER_EVIDENCE_NOTE

SECRET_LINE = re.compile(r"(?im)^.*(?:authorization\s*[:=]|bearer\s+|(?:api[_ -]?key|password|passwd|密码|令牌|secret|access[_-]?token|cookie)\s*[:：=]).*$")
SECRET_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)")
MEMORY_BLOCK = re.compile(r"<(personal_memory_data|personal_conversation_data|personal_memory_coverage|personal_task_status_context)>.*?</\1>", re.S)


def clean_text(text):
    return SECRET_VALUE.sub("[REDACTED]", SECRET_LINE.sub("[REDACTED]", MEMORY_BLOCK.sub("", text)))


# These are source labels, not an attempt to infer whether an arbitrary task
# is fictional. Match only explicit synthetic annotations in the same paragraph.
_SYNTHETIC_LABEL = re.compile(
    r"(?:以下|下面)(?:是|为)(?:新增的|一条)?合成测试(?:资料|记录|计划)"
    r"|(?:这些|这)(?:都|仍)?是(?:一条)?合成测试(?:资料|记录|计划)"
    r"|(?:请)?记住一条[^。！？\n]{0,30}合成测试(?:资料|记录|计划)")
_NO_EXECUTION = re.compile(r"(?:不需要|无需|不要)(?:实际)?执行")

def synthetic_qualifiers(text, quote):
    if not isinstance(text,str) or not isinstance(quote,str) or not quote:
        return {}
    start=text.find(quote)
    if start<0 or text.find(quote,start+1)>=0:
        return {}
    # A blank line ends the annotation's paragraph; another user's message
    # or assistant text is never passed here as the source of this quote.
    left=text.rfind("\n\n",0,start)+2 if "\n\n" in text[:start] else 0
    right=text.find("\n\n",start+len(quote))
    paragraph=text[left:right if right>=0 else len(text)]
    if len(paragraph)>4000:
        return {}
    label=_SYNTHETIC_LABEL.search(paragraph)
    if not label:
        return {}
    # An introductory "following" label cannot label an earlier assertion.
    if label.group().startswith(("以下","下面","请记住","记住")) and left+label.start()>start:
        return {}
    no_execution=_NO_EXECUTION.search(paragraph)
    quotes=[label.group()]
    if no_execution:quotes.append(no_execution.group())
    return {"synthetic":True,"no_execution":bool(no_execution),"qualifying_quotes":quotes}

def evidence_context_note(items):
    if not items:return ""
    tool_note='工具执行观察只证明工具返回了记录中的内容，不等于助手推断或用户确认，也不证明业务任务已完成。tool_observation.state=result仅表示收到结果，不能据此说执行成功；error表示工具报错，不能把其中的通过片段说成整体成功。' if any(m.get("source_role")=="tool" or m.get("source_qualifiers",{}).get("tool_observation") for m in items) else ""
    return source_identity_note(source_identity_summary(items)) + ANSWER_EVIDENCE_NOTE + tool_note + ("来源说明是证据的一部分，不能省略。source_qualifiers.synthetic=true表示合成测试资料，"
            "回答这些内容时必须明确标注为测试记录，不能当作真实业务进度或实际待办；"
            "no_execution=true表示来源要求不实际执行，不是已经核实的未执行结果；实际是否执行没有观察证据时应说未知。没有该标记也不授予执行权限。"
            "用户明确报告本次新增、修复或已验证的变化，可以作为来源报告的事实复述，并注明据用户描述；缺少独立验收不等于来源没有报告完成，不能反向否定已报告的事实。"
            "后续计划只说明意向，不证明实际尚未开始、尚未实施或尚未交付；执行阶段未明确报告就说未知，不补写这些否定结论。"
            "只有来源明确标注时才称为合成测试资料，不能仅因测试环境就把其他用户原文强行标为测试记录。"
            "已完成一次检查不等于以后无需重做；任务未完成不等于没有部分成果，"
            "也不能据此推断核对结果尚未产生、尚未开始或安排已自动作废。"
            "来源没有说明的进度、结果和后续安排应直说未知，不补充结论；未提供记录不等于事情不存在，不能写成没有其他进度或没有后续安排。非计划的校验词记录不适用取消状态，不称为未被取消的计划。\n")


# A prompt that names a time, a file, a client or asks who did something is a
# new question, not a continuation of the previous turns. Those anchors must
# reach the archive search undiluted; "继续"/"那这个呢" still need recent text.
_ARTIFACT_CUE = re.compile(
    r"(?<![A-Za-z0-9_.])([A-Za-z0-9_][A-Za-z0-9_\-./\\]*\."
    r"(?:py|js|mjs|ts|tsx|vue|json|md|sql|ps1|cmd|sh|toml|ya?ml))(?![A-Za-z0-9_])", re.I)
_DATE_TIME_CUE = re.compile(
    r"(?<!\d)(?:(\d{4})[-/](\d{1,2})[-/](\d{1,2})|(\d{1,2})[-/月](\d{1,2})[日号]?)"
    r"(?:\s*(\d{1,2}):(\d{2}))?(?!\d)")
# Deictic words ("当时"/"上次"/"刚才") point *at* the recent turns and must
# keep using them; only anchors that recent turns cannot supply count here.
_PROVENANCE_CUE = re.compile(
    r"谁改|谁写|谁动|谁做|哪次|什么时候|几点|为什么改|怎么改的?|哪个客户端|"
    r"来源|出自|昨晚|那天|昨天|前天|今天|今晚", re.I)
# Only clients the archive actually records; display names are not app values.
_APP_CUE = {"workbuddy": re.compile(r"(?<![A-Za-z])workbuddy(?![A-Za-z])", re.I),
            "codex": re.compile(r"(?<![A-Za-z])codex(?![A-Za-z])", re.I),
            "claude": re.compile(r"(?<![A-Za-z])claude(?:\s+code)?(?![A-Za-z])", re.I)}


def _date_cue(text):
    """Return the first date/time match, or None for ranges like "3-5 条".

    A bare "3-5" is far more often a count than a date; require a four-digit
    year, an explicit 月/日/号 marker, or a clock time before treating it as one.
    """
    for m in _DATE_TIME_CUE.finditer(text):
        if m.group(1):
            if 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
                return m
            continue
        token = m.group(0)
        if m.group(6) is None and not any(x in token for x in ("月", "日", "号")):
            continue
        if 1 <= int(m.group(4)) <= 12 and 1 <= int(m.group(5)) <= 31:
            return m
    return None


def _time_window(match):
    """UTC bounds for the archive's captured_at, or None.

    The user speaks in local time and the archive stores UTC, so the anchor is
    converted rather than compared literally. A clock time gets one hour on
    each side: the change may be described before or after it happened.
    """
    if match is None:
        return None
    try:
        if match.group(1):
            year = int(match.group(1))
            month, day = int(match.group(2)), int(match.group(3))
        else:
            year = datetime.now().year
            month, day = int(match.group(4)), int(match.group(5))
        if match.group(6) is not None:
            base = datetime(year, month, day, int(match.group(6)), int(match.group(7)))
            start, end = base - timedelta(minutes=60), base + timedelta(minutes=60)
        else:
            start, end = datetime(year, month, day), datetime(year, month, day, 23, 59, 59)
    except ValueError:
        return None
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


def parse_episodic_cues(text):
    raw = text or ""
    match = _date_cue(raw)
    date_cue = match.group(0) if match else ""
    artifacts = [m.group(1) for m in _ARTIFACT_CUE.finditer(raw)][:3]
    apps = sorted(name for name, pattern in _APP_CUE.items() if pattern.search(raw))
    is_provenance = bool(_PROVENANCE_CUE.search(raw))
    clean = raw
    if date_cue:
        clean = (raw[:raw.find(date_cue)] + " " + raw[raw.find(date_cue) + len(date_cue):]).strip()
    return {"date_cue": date_cue, "time_window": _time_window(match), "artifacts": artifacts,
            "app_mentions": apps,
            "is_provenance_query": is_provenance, "fts_clean_query": clean or raw,
            "has_specific_cue": bool(date_cue or artifacts or apps or is_provenance)}


def retrieval_coverage(items, available=True, *, archive_state="not_needed",
                       archive_found_count=0, archive_query_terms=None):
    # A small authorized retrieval cannot prove that an entire library has
    # been enumerated, even when it happens to return all known test rows.
    coverage = {"exhaustive":False,"provided_memory_count":len(items),
            "memory_limit":5,"availability":"available" if available else "unavailable"}
    # Whether the archive was searched at all is a fact about this turn, not a
    # confidence score: "searched and found nothing" must not look like "never
    # looked". Only the state is reported, never unauthorized archive text.
    if archive_state != "not_needed" or archive_found_count:
        coverage["conversation_archive_state"] = archive_state
        coverage["conversation_archive_found_count"] = int(archive_found_count)
    if archive_state.startswith("searched") and archive_query_terms:
        coverage["conversation_archive_query_terms"] = list(archive_query_terms)[:8]
    return coverage

def retrieval_coverage_context(coverage):
    data=json.dumps(coverage,ensure_ascii=False,separators=(",",":"))
    state=coverage.get("conversation_archive_state","")
    archive_note={"searched_0":"本轮已按授权范围检索过历史会话归档，没有可用的命中；这不证明没有记录。\n",
                  "not_granted":"本轮没有读取历史会话归档的权限，因此没有检索；不能据此断言没有记录。\n",
                  "read_unavailable":"本轮尝试检索历史会话归档但读取失败，没有得到结果。\n"}.get(state,"")
    return ("\n以下是本轮记忆检索的覆盖范围，不是全库清单。exhaustive=false表示只提供有限检索结果，"
            "也可能因权限、相关性、状态或篇幅限制遗漏记录；零条结果或读取不可用都不证明不存在。"
            "只说明本轮提供的记录中找到了什么；没有另行取得明确的全量覆盖证据时，"
            "不要断言仅剩、只有这些、没有其他记录或全部事项已完成，也不要假称已遍历全库。"
            "需要穷尽清单时应说明当前证据不足。不要向用户复述内部字段名、XML或JSON，"
            "用普通语言说明本轮提供的记录可能不全。\n"
            + archive_note
            + "<personal_memory_coverage>"+data+"</personal_memory_coverage>")


# This is a conservative extraction view, not the durable conversation archive.
# Masking keeps character offsets and untouched user evidence byte-for-byte.
_UI_REPLY = re.compile(r"<send_user_message_question_reply\b[^>]*>(?P<body>.*?)(?:</send_user_message_question_reply\s*>|\Z)", re.S | re.I)
_MATERIAL = re.compile(r"```.*?(?:```|\Z)|<(?P<tag>document|untrusted_text|source|material|log|code)\b[^>]*>.*?(?:</(?P=tag)>|\Z)", re.S | re.I)
_READBACK_HEAD = re.compile(
    r"\s*(?:(?:根据)?(?:本轮|本次)(?:检索到的|提供的)?记忆(?:里|中|[，,])"
    r"|(?:以下|下面)(?:清单|摘要|报告)(?:来自|依据)"
    r"|(?:以下|下面)是[^。！？\n]{0,24}(?:模型|助手|客户端)(?:的)?(?:回复|回答|输出)[：:])")
_NEW_INTENT = re.compile(r"(?m)^(?:引用结束[：:]?|我的(?:新安排|新计划|决定)[：:]|(?:我|我们)(?:重新)?(?:计划|决定|安排|准备|打算)|(?:请)?记住|以后|今后|从现在)")
_LASTING_ANSWER = re.compile(r"^(?:以后|今后|从现在|所有(?:回答|回复)|每次(?:回答|回复)|(?:请)?记住|(?:我|我们)(?:重新)?(?:计划|决定|安排|打算))")
_ONE_OFF_CONSENT = re.compile(r"(?m)^[ \t]*(?:(?:我)?(?:允许|同意|批准|授权|不允许|不同意|拒绝)(?:本次|这次|此次|本轮)|(?:本次|这次|此次|本轮)(?:允许|同意|批准|授权|不允许|不同意|拒绝))[^。！？\n]*[。！？]?")


def extraction_text(text):
    """Exclude known transport/material; keep explicit independent new intent.

    Undelimited prose is not universally attributable. Recognize bounded report
    prefaces; don't guess authorship from a provider name or a task identifier.
    Ambiguous UI shorthand stays in the archive, not a fabricated preference.
    """
    chars=list(text)
    def mask(start,end):
        for i in range(start,end):
            if chars[i] not in '\r\n':chars[i]=' '
    materials=list(_MATERIAL.finditer(text))
    for match in materials:mask(*match.span())
    for match in _UI_REPLY.finditer(text):
        mask(*match.span())
        if any(m.start()<match.end() and match.start()<m.end() for m in materials):continue
        body=match['body']
        try:
            rows=json.loads(body)
            if not isinstance(rows,list) or not all(isinstance(r,dict) and isinstance(r.get('answer'),str) for r in rows):continue
        except (ValueError,TypeError):continue
        # Restore only an independently explicit, lasting answer, verbatim.
        # Question text, IDs, consent, JSON syntax and escaped reconstructions
        # cannot be promoted into a user's long-term assertion.
        for value in re.finditer(r'"answer"\s*:\s*"(?P<answer>(?:\\.|[^"\\])*)"',body):
            answer=value['answer']
            if '\\' in answer or not _LASTING_ANSWER.match(answer):continue
            if not any(r['answer']==answer for r in rows):continue
            start=match.start('body')+value.start('answer');end=start+len(answer)
            chars[start:end]=text[start:end]
    head=_READBACK_HEAD.match(text)
    if head:
        # An explicit new user paragraph after the report is independently
        # evaluated. Report questions/conditionals do not silently become intent.
        tail=_NEW_INTENT.search(text,head.end())
        mask(0,tail.start() if tail else len(text))
    # Standalone annotation is source metadata, not a new durable decision.
    for match in re.finditer(r"(?m)(?<![^。！？\n])(?:这|这些)(?:仍|都)?是合成测试(?:记录|资料)[，,](?:不需要|无需)(?:实际)?执行(?:[。！？]|(?=\n|$))",text):
        mask(*match.span())
    for match in _ONE_OFF_CONSENT.finditer(text):
        # Preserve a separately stated future agreement following consent.
        tail=re.search(r'(?:但|[，；;])\s*(?:以后|今后|从现在)',match.group())
        mask(match.start(),match.start()+tail.start() if tail else match.end())
    return ''.join(chars)


def capturable_quote(text,quote):
    if not isinstance(text,str) or not isinstance(quote,str) or not quote:return False
    view=extraction_text(text)
    starts=[m.start() for m in re.finditer(re.escape(quote),text)]
    return bool(starts) and all(view[start:start+len(quote)]==quote for start in starts)
