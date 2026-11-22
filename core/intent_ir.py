"""Versioned intermediate representation between natural language and business services."""
from dataclasses import dataclass
from typing import Any
import re
from core.time_input import NUMBER, number
from core.rail_contract import SEAT_CLASSES, SEAT_REQUIREMENTS, PRICE_PREFERENCES


COMMAND_SCHEMA_VERSION = 'rule-intent-v1'

SUPPORTED_ACTIONS = {'none', 'clarify', 'read', 'create', 'update', 'delete', 'confirm', 'cancel'}
SUPPORTED_DOMAINS = {'hotel', 'trip', 'research', 'reminder', 'booking', 'weather', 'forecast', 'route', 'traffic',
                     'places', 'walking', 'transit', 'rail', 'document', 'system', 'preferences', 'search'}
FORBIDDEN_MODEL_KEYS = {'trip_id', 'reminder_id', 'plan_code', 'item_code', 'owner_id', 'group_id', 'platform', 'scope_id', 'principal_id', 'account_id', 'user_id'}
READ_OPERATION_FIELDS = {'weather':{'location'},'forecast':{'location'},'route':{'origin','destination'},
    'hotel':{'mode','targets','discovery','reference','stay','budget','source_spans','proposal_response'},
    'traffic':{'origin','destination'},'walking':{'origin','destination'},'transit':{'origin','destination','city'},
    'places':{'city','keywords'}, 'rail':{'origin','destination','travel_date','origin_scope','destination_scope',
        'train_preference','train_preference_text','departure_window','seat_class','seat_requirement','price_preference'}}
RAIL_ENUMS = {'origin_scope': {'city','station'}, 'destination_scope': {'city','station'},
              'train_preference': {'high_speed','high_speed_only','d_train','conventional','all'},
              'seat_class':set(SEAT_CLASSES),'seat_requirement':SEAT_REQUIREMENTS,'price_preference':PRICE_PREFERENCES}
DERIVED_READ_FIELDS = {'transit': {'city'}}
TRIP_READ_OPERATIONS = {'view_current_trip':'查看行程','check_prices':'查询候选价格','check_budget':'查询行程预算'}


def trip_read_check(check, source_text, *, budget=False):
    if (not isinstance(check,dict) or check.keys()-{'scope','source','names','kind','day_index','meal','mode','hotel_stay'}
            or not isinstance(check.get('source'),str) or not check['source'] or check['source'] not in source_text):
        raise ValueError('行程读取字段必须保留本轮原话。')
    value=dict(check);source=value['source']
    mode=value.get('mode','saved')
    if mode not in {'saved','lookup','refresh'} or mode!='saved' and (value.get('scope')!='accommodation' or budget or re.search(r'不(?:要)?(?:再)?(?:查|刷新)|不联网|只(?:解释|看|读).*(?:旧|之前)',source)):
        raise ValueError('主动查价需要本轮明确的住宿查询依据。')
    # Older IR used check_prices for budget questions; normalize its explicit evidence.
    value.setdefault('kind','budget' if budget or '预算' in source and not value.get('names')
                     and not re.search(r'价格|报价|房价|票价',source) else 'price')
    allowed={'food','accommodation','transport','trip'} if value['kind']=='budget' else {'food','accommodation'}
    if (not isinstance(value['kind'],str) or value['kind'] not in {'price','budget'}
            or not isinstance(value.get('scope'),str) or value['scope'] not in allowed):
        raise ValueError('预算与候选价格的读取范围无效。')
    if value['kind']=='budget' and mode!='saved':raise ValueError('预算读取不触发报价刷新。')
    if 'hotel_stay' in value:
        from tools.trip.constraints import stay_change
        if mode=='saved' or value['scope']!='accommodation':raise ValueError('临时入住条件仅用于明确主动查价。')
        stay_change({},value['hotel_stay'],source_text)
    names=value.get('names',[])
    if (not isinstance(names,list) or len(names)>12 or any(not isinstance(n,str) or not n or n not in source_text for n in names)):
        raise ValueError('指定商家必须来自本轮原话。')
    days={number(v) for v in re.findall(r'第('+NUMBER+r')[天日]',source)}
    meals={word.replace('饭','餐') for word in re.findall(r'早餐|午餐|晚餐|早饭|午饭|晚饭',source)}
    if len(days)==1:value.setdefault('day_index',next(iter(days)))
    if len(meals)==1:value.setdefault('meal',next(iter(meals)))
    if ('day_index' in value and (type(value['day_index']) is not int or not 1<=value['day_index']<=14 or value['day_index'] not in days)
            or 'meal' in value and (not isinstance(value['meal'],str) or value['meal'] not in meals)):
        raise ValueError('查询日期和餐次必须对应原话。')
    if (days and 'day_index' not in value or meals and 'meal' not in value):
        raise ValueError('多个日期或餐次需分别列出读取事项。')
    if value['scope']!='food' and ('day_index' in value or 'meal' in value):
        raise ValueError('单餐读取只适用于餐饮。')
    return value


def _contains_forbidden(value):
    if isinstance(value, dict):
        return any(key in FORBIDDEN_MODEL_KEYS or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


@dataclass(frozen=True)
class IntentOperation:
    domain: str
    operation: str
    values: dict[str, Any]


@dataclass(frozen=True)
class IntentIR:
    schema_version: int
    action: str
    operations: tuple[IntentOperation, ...]
    missing_fields: tuple[str, ...] = ()
    requires_confirmation: bool = False
    source_spans: tuple[str, ...] = ()
    confidence: float | None = None

    @property
    def actionable(self) -> bool:
        return self.action not in {'none', 'clarify'} and bool(self.operations)


def parse_intent_ir(value: Any, source_text: str) -> IntentIR:
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('语义解析版本无效。')
    action = value.get('action') or value.get('intent')
    action = {'modify': 'update', 'edit': 'update', 'question': 'clarify', 'execute': 'confirm'}.get(action, action)
    if action not in SUPPORTED_ACTIONS:
        raise ValueError('语义解析动作无效。')
    raw_operations = value.get('operations', [])
    if not isinstance(raw_operations, list) or len(raw_operations) > 8:
        raise ValueError('语义解析操作数量无效。')
    operations = []
    for raw in raw_operations:
        if not isinstance(raw, dict) or raw.get('domain') not in SUPPORTED_DOMAINS:
            raise ValueError('语义解析领域无效。')
        operation = raw.get('operation')
        operation = {'remove_trip_activity': 'remove_activity', 'delete_activity': 'remove_activity',
                     'view_trip': 'view_current_trip', 'confirm_trip': 'confirm_pending_operation'}.get(operation, operation)
        if not isinstance(operation, str) or not operation or len(operation) > 80:
            raise ValueError('语义解析操作无效。')
        values = {key: item for key, item in raw.items() if key not in {'domain', 'operation'}}
        if _contains_forbidden(values):
            raise ValueError('语义解析不能生成资源编号或身份字段。')
        if raw['domain'] == 'preferences':
            from core.preference_contract import preference_change
            if operation not in {'update','suggest','forget'}:
                raise ValueError('偏好操作无效。')
            values = preference_change(values, source_text)
        if raw['domain'] == 'research' and operation == 'search_travel_guides':
            from core.research_contract import project_request
            project_request(values, source_text)
        if raw['domain'] == 'trip' and isinstance(values.get('changes'), dict) and 'intercity' in values['changes']:
            from core.intercity_contract import intercity_request, IntercityContractError
            change = values['changes']['intercity']
            if (not isinstance(change, dict) or set(change) != {'value', 'source'}
                    or not isinstance(change['value'], dict) or not isinstance(change['source'], str)
                    or not change['source'] or len(change['source']) > 240 or change['source'] not in source_text):
                raise IntercityContractError('invalid_evidence', 'intercity')
            intercity_request(change['value'], {}, source_text)
        if raw['domain']=='trip' and operation in TRIP_READ_OPERATIONS:
            if (values.keys()-{'target_ref','trip_index','trip_reference','requested_checks','include_trip'}
                    or 'include_trip' in values and type(values['include_trip']) is not bool):
                raise ValueError('只读行程操作不能包含修改或保存字段。')
        if raw['domain']=='trip' and operation in {*TRIP_READ_OPERATIONS,'update_constraints'} and 'requested_checks' in values:
            checks=values['requested_checks']
            if not isinstance(checks,list) or not 1<=len(checks)<=8:
                raise ValueError('行程读取事项须为1至8项。')
            values['requested_checks']=[trip_read_check(check,source_text,budget=operation=='check_budget') for check in checks]
        if raw['domain'] == 'search':
            fields = {'name','city'} if operation=='query_official_website' else set()
            if (operation not in {'query_official_website','continue_official_websites'} or values.keys()-fields
                    or any(not isinstance(v,str) or not v or len(v)>150 or v not in source_text for v in values.values())):
                raise ValueError('官网查询字段必须与用户原话对应，合法字段为 name、city。')
        if raw['domain']=='hotel':
            from core.hotel_contract import validate_request
            if operation!='query' or action not in {'read','clarify'}:raise ValueError('酒店查价仅支持只读query。')
            values=validate_request(values)
        elif raw['domain'] in READ_OPERATION_FIELDS and operation in {'query','search'}:
            if (values.keys()-READ_OPERATION_FIELDS[raw['domain']]
                    or any(not isinstance(item,str) or len(item)>150 for item in values.values())):
                raise ValueError('只读查询含不支持字段或非法参数；需要重新解析。')
            if raw['domain']=='rail':
                if any(values[key] not in allowed for key,allowed in RAIL_ENUMS.items() if key in values):
                    raise ValueError('铁路地点范围或车次类别无效。')
                if values.get('train_preference') and not values.get('train_preference_text'):
                    raise ValueError('明确车次偏好必须保留本轮原话；未指定时省略偏好，采用服务端默认。')
        operations.append(IntentOperation(raw['domain'], operation, values))
    missing = value.get('missing_fields', [])
    spans = value.get('source_spans', [])
    if not isinstance(missing, list) or any(not isinstance(item, str) or len(item) > 80 for item in missing):
        raise ValueError('语义解析缺失字段无效。')
    if not isinstance(spans, list) or len(spans) > 20 or any(not isinstance(item, str) or not item or len(item) > 200 for item in spans):
        raise ValueError('语义解析原文依据无效。')
    if any(span not in source_text for span in spans):
        raise ValueError('语义解析原文依据无法对应用户消息。')
    confirmation = value.get('requires_confirmation', False)
    if type(confirmation) is not bool:
        raise ValueError('语义解析确认标记无效。')
    if confirmation and action in {'read','clarify'} and operations and all(op.domain=='hotel' for op in operations):
        raise ValueError('酒店查询是只读操作，requires_confirmation应为false；缺少入住条件交由工具补问，不表示确认保存。')
    confidence = value.get('confidence')
    if confidence is not None and (type(confidence) not in {int, float} or not 0 <= confidence <= 1):
        raise ValueError('语义解析置信度无效。')
    if action == 'none' and operations:
        raise ValueError('无动作语义不能包含操作。')
    requested_domains=value.get('requested_domains',[])
    if not isinstance(requested_domains,list):raise ValueError('需求领域须为数组。')
    if action=='none' and 'hotel' in requested_domains:
        raise ValueError('已识别酒店查价需求，须保留hotel/query或待澄清事项，不得以none结束。')
    if action not in {'none', 'clarify'} and not operations and not missing:
        raise ValueError('有动作语义必须包含操作或缺失字段。')
    return IntentIR(1, action, tuple(operations), tuple(missing), confirmation, tuple(spans), confidence)


def to_jsonable(ir: IntentIR) -> dict[str, Any]:
    return {'schema_version': ir.schema_version, 'action': ir.action,
            'operations': [{'domain': operation.domain, 'operation': operation.operation, **operation.values}
                           for operation in ir.operations],
            'missing_fields': list(ir.missing_fields), 'requires_confirmation': ir.requires_confirmation,
            'source_spans': list(ir.source_spans), 'confidence': ir.confidence}
