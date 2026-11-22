"""Validated research request projection and bounded output contract."""
import re

MAX_ITEMS = 8
TARGET_TEXT_CHARS = 300
MAX_TEXT_CHARS = 600

SCHEMA_VERSION = 'research-request-v2'
TOPICS = {'ticket_price': '票价', 'opening_hours': '开放时间', 'reservation_rules': '预约规则'}
REQUEST_FIELDS = frozenset({'query_text', 'source_request', 'plan_index', 'plan_reference',
                            'topics', 'arrival_date_text', 'visit_start_date_text'})


# A conservative fallback for a bare follow-up. Explicit place/city names do not
# match this whole-sentence grammar and are left to the normal semantic parser.
RESEARCH_REFERENCE_FILLER = (
    r'(?:那么|那|再|请|麻烦|帮我|为我|给我|我想|想|知道|分别|详细|介绍|讲讲|说说|查询|查看|了解|一下|核验|核对|'
    r'的|信息|情况|具体|文化背景|历史背景|看点|玩法|开放时间|门票|票价|预约要求|预约规则|和|及|等|吧|呢|[，,、。？！?!\s])*')
BARE_RESEARCH_REFERENCE = re.compile(
    r'^'+RESEARCH_REFERENCE_FILLER+
    r'(?P<reference>(?:这些|那些|上述|上面的|刚才的)(?:景点|地点|地方)|'
    r'(?:这个|该|当前|这份|刚才确认的|刚才修改的)(?:行程|方案)|它们)'
    +RESEARCH_REFERENCE_FILLER+r'$'
)


def infer_plan_reference(source_text):
    match = BARE_RESEARCH_REFERENCE.fullmatch(source_text.strip())
    return match['reference'] if match else ''


class ResearchRequestError(ValueError):
    """A core schema conflict; inert extension fields never raise this error."""
    def __init__(self, code):
        self.code = code
        super().__init__('研究请求校验失败：' + code)


def project_request(values, source_text):
    """Retain raw IR at the caller; only this explicit projection may execute."""
    from core.intent_ir import _contains_forbidden
    if not isinstance(values, dict) or _contains_forbidden(values):
        raise ResearchRequestError('forbidden_identity')
    if values.keys() & {'artifact_id', 'resource_id', 'permissions', 'actions', 'operations'}:
        raise ResearchRequestError('forbidden_action')
    raw_keys = values.keys()
    values = dict(values)
    adopted_aliases = set()
    for alias, canonical in {'plan_ref':'plan_reference','arrival_date':'arrival_date_text',
                              'visit_start_date':'visit_start_date_text'}.items():
        if canonical not in values and alias in values:
            values[canonical] = values[alias]
            adopted_aliases.add(alias)
    result = {}
    for key in REQUEST_FIELDS - {'topics', 'plan_index'}:
        value = values.get(key, '')
        if not isinstance(value, str) or len(value) > 500 or (value and value not in source_text):
            raise ResearchRequestError('ungrounded_' + key)
        if value:
            result[key] = value
    if not result.get('plan_reference'):
        reference = infer_plan_reference(source_text)
        if reference:
            result['plan_reference'] = reference
    if not result.get('query_text'):
        raise ResearchRequestError('missing_query_text')
    if 'plan_index' in values:
        index = values['plan_index']
        if type(index) is not int or index < 1:
            raise ResearchRequestError('invalid_plan_index')
        result['plan_index'] = index
    topics = values.get('topics', [])
    if not isinstance(topics, list) or any(not isinstance(t, str) or t not in TOPICS for t in topics):
        raise ResearchRequestError('invalid_topics')
    if topics:
        result['topics'] = list(dict.fromkeys(topics))
    return result, sorted(raw_keys - REQUEST_FIELDS - adopted_aliases)
