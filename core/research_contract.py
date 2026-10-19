"""Validated research request projection and bounded output contract."""
MAX_ITEMS = 8
TARGET_TEXT_CHARS = 300
MAX_TEXT_CHARS = 600

SCHEMA_VERSION = 'research-request-v2'
TOPICS = {'ticket_price': '票价', 'opening_hours': '开放时间', 'reservation_rules': '预约规则'}
REQUEST_FIELDS = frozenset({'query_text', 'source_request', 'plan_index', 'plan_reference',
                            'topics', 'arrival_date_text', 'visit_start_date_text'})


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
