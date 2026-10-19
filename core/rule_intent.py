"""Conservative whole-request grammar. Parsing never performs business operations."""
from dataclasses import dataclass
import os
import re

from core.intent_ir import IntentIR, IntentOperation, parse_intent_ir, to_jsonable

VERSION = 'rule-intent-v1'


def enabled():
    return os.getenv('AI_RULE_ROUTING', 'true').lower() == 'true'


def compound_boundary(text):
    # Punctuation in narrative memory is not an instruction to execute several tasks.
    if not re.match(r'^(?:请|帮我)?(?:查询|查一下|查天气|查路线|搜索|规划|取消|删除|查看|修改行程|酒店预算(?:改为|改成)|不要夜游|不安排夜游)',text.strip()):
        return False
    return bool(re.search(r'[；;\n]|(?:[，,]\s*|同时|顺便|并且|还有)(?:请|再|帮我|顺便|同时)?(?:查|提醒|取消|删除|搜索|推荐|规划|把)',text))


@dataclass(frozen=True)
class RuleResult:
    status: str
    ir: IntentIR | None = None
    reason: str = 'no_match'
    rule_ids: tuple[str, ...] = ()


def location(value):
    # Free-text slots must not swallow a second action, temporal qualifier or quotation.
    return bool(re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9· ]{2,45}', value)
        and not re.search(r'查询|天气|路线|提醒|预约|取消|不要|不查|如果|然后|还有|同时|顺便|并且|以及|和|或者|还是|明天|后天|今天|小时|今晚|现在|怎么|什么|解释|意思|改成|改为|推荐|帮我|规划|安排|以后|那[里个]|这[里个]', value))


def parse_rules(text):
    if len(text) > 1000 or re.search(r'[“”"\'（）()\[\]：:]|https?://', text):
        return RuleResult('fallback', reason='unsupported_structure')
    parts = [part.strip() for part in re.split(r'[；;。\n]', text.strip()) if part.strip()]
    if not 1 <= len(parts) <= 3:
        return RuleResult('fallback', reason='operation_limit')
    operations, spans, rules, changes = [], [], [], {}
    for part in parts:
        match = re.fullmatch(r'(?:请)?查询天气\s*(.+?)|(.+?)现在天气怎么样[？?]?', part)
        if match and location((match[1] or match[2]).strip()):
            value = (match[1] or match[2]).strip()
            operations.append(IntentOperation('weather', 'query', {'location':value}))
            spans.append(part); rules.append('current_weather'); continue
        match = re.fullmatch(r'(?:请)?查询(?:驾车)?路线\s*(.+?)\s*(?:→|->)\s*(.+)', part)
        if not match:
            match = re.fullmatch(r'从(.+?)到(.+?)开车怎么走[？?]?',part)
        if match and all(location(value.strip()) for value in match.groups()):
            operations.append(IntentOperation('route','query',{'origin':match[1].strip(),'destination':match[2].strip()}))
            spans.append(part); rules.append('driving_route'); continue
        match = re.fullmatch(r'酒店预算(?:改为|改成)([1-9][0-9]{0,5}(?:-[1-9][0-9]{0,5})?)(元/间/晚|人民币每间每晚)', part)
        if match and 'budget' not in changes:
            amounts=[int(v) for v in match[1].split('-')]
            if len(amounts)==2 and amounts[0]>amounts[1]:
                return RuleResult('fallback',reason='invalid_budget_range')
            changes['budget'] = {'value':match[1],'scope':'accommodation','unit_text':match[2],'source':part}
            spans.append(part); rules.append('hotel_budget'); continue
        if part in {'不要夜游','不安排夜游'} and 'night' not in changes:
            changes['night'] = {'value':False,'source':part}
            spans.append(part); rules.append('no_night'); continue
        return RuleResult('fallback', reason='uncovered_text', rule_ids=tuple(rules))
    if changes and operations:
        return RuleResult('fallback', reason='mixed_read_write', rule_ids=tuple(rules))
    if changes:
        operations = [IntentOperation('trip','update_constraints',{'target_ref':'current_trip','changes':changes})]
    ir = IntentIR(1,'update' if changes else 'read',tuple(operations),requires_confirmation=bool(changes),source_spans=tuple(spans))
    return RuleResult('complete',parse_intent_ir(to_jsonable(ir),text),'complete',tuple(rules))


def unsafe_command_tail(text):
    """Legacy prefix commands may only bypass semantics for one bounded command."""
    if not re.match(r'/?(?:查询天气|天气预报|查询路线|查询路况)', text.strip()):
        return False
    value=text.strip().lstrip('/').rstrip('。')
    if re.search(r'[，,；;。\n]|顺便|同时|并且|然后|提醒|取消|如果|不要|解释|意思',value):
        return True
    match=re.fullmatch(r'(?:查询天气|天气预报)\s*(.*)',value)
    if match:
        return bool(match[1].strip()) and not location(match[1].strip())
    match=re.fullmatch(r'(?:查询路线|查询路况)\s*(.*?)\s*(?:→|->|到|至)\s*(.*)',value)
    return bool(match and any(v.strip() and not location(v.strip()) for v in match.groups()))
