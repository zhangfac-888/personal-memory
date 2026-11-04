"""Conservative formatting hints compiled only from applicable, source-grounded rules.

This is model input, not an output interceptor or a guarantee of host compliance.
"""
import json,re

_QUOTED = r'[“「\"\']([^”」\"\'<>\r\n]{1,80})[”」\"\']'
_OVERRIDE = re.compile(r'(?:只|仅)(?:回复|输出)|(?:忽略|覆盖).{0,12}(?:约定|格式)|(?:使用|用|输出)\s*JSON',re.I)

_TASK_SCOPE = re.compile(r'(?:仅在|只在|仅用于|只用于)(?:(?:当前|这个|本)项目)?\s*'+_QUOTED+r'\s*(?:任务|场景)')

def rule_applies(rule, prompt):
    """A retrieved project rule is not automatically valid for every task."""
    condition = _TASK_SCOPE.search(rule)
    if condition:
        return condition.group(1).casefold() in prompt.casefold()
    # Unknown explicit task scopes remain unavailable, rather than guessing.
    if re.search(r'(?:仅在|只在|仅用于|只用于).{0,120}(?:任务|场景)', rule):
        return False
    return True


def _change_summary_contract(rule,prompt):
    # Compile only an explicit applicable task and explicit count. No task
    # identifiers, source facts or example answers are encoded in this grammar.
    task=re.search(r'(发布说明|交付简报|验收摘要)统一',rule)
    if not task or task[1] not in prompt:return None
    prefix=re.search(r'以([^，。\n]{1,40}?)开头[，,](?:然后|再)(?:列出两项变化|用两条编号列出验证事项)',rule)
    status=re.search(r'先写([^，。\n]{1,40}?)[，,]再列出两项变化',rule)
    if not prefix and not status:return None
    header=(prefix or status)[1].strip()
    quoted=re.fullmatch(_QUOTED,header)
    if quoted:header=quoted[1]
    numbered=bool(prefix and '用两条编号列出验证事项' in rule)
    return {'nonempty_lines':3,'first_line_prefix':header,
            'remaining_line_prefixes':['1.','2.'] if numbered else ['-','-'],
            'extra_paragraphs':False}

def parse_contract(rule, prompt):
    if _OVERRIDE.search(prompt) or re.search(r'可能|或许|假设|例如|比如|尽量|如果|只有|除非',rule):
        return None
    if not rule_applies(rule, prompt):
        return None
    header=re.search(r'第一行(?:单独)?写\s*'+_QUOTED,rule)
    body=re.search(r'(?:接着|然后)\s*分别用\s*'+_QUOTED+r'\s*(?:和|、)\s*'+_QUOTED+r'\s*两行回答',rule)
    if not header or not body:
        return _change_summary_contract(rule,prompt)
    if header.group(1) in body.groups():return None
    return {'nonempty_lines':3,'first_line_exact':header.group(1),
            'remaining_line_prefixes':[body.group(1),body.group(2)],'extra_paragraphs':False}

def output_contract(rules,prompt):
    found=[]
    for rule in rules:
        contract=parse_contract(rule,prompt)
        if contract and contract not in found:found.append(contract)
    # Contradictory derived structures must never be resolved by hidden ordering.
    return found[0] if len(found)==1 else None

def render_contract(rules,prompt):
    contract=output_contract(rules,prompt)
    if not contract:return ''
    data=json.dumps(contract,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e')
    return ('\n当前用户本轮明确要求优先。以下结构仅来自上方适用约定，不增加事实：'
            '整份回答严格使用规定的非空行；需要项目代号或缺失信息说明时，放在对应内容行内，'
            '不在这些行之外另加段落、标题或解释。\n<output_format_contract>'+data+'</output_format_contract>')

def check_contract(answer,contract):
    lines=[line.strip() for line in answer.splitlines() if line.strip()]
    if len(lines)!=contract['nonempty_lines']:return False
    header_ok=(lines[0]==contract['first_line_exact']) if 'first_line_exact' in contract else lines[0].startswith(contract['first_line_prefix'])
    return (header_ok and
            all(line.startswith(prefix) for line,prefix in zip(lines[1:],contract['remaining_line_prefixes'])))
