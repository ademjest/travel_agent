"""Explicit tool ownership and dispatch. Schemas do not grant user authorization."""
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType

from core.tool_result import (ToolExecutor, ToolResult, validate_arguments,
                              LEGACY_CATALOG, COMPACT_CATALOG, DEFAULT_CATALOG)
from core.tool_result import TOOLS_BY_NAME, READ_TOOL_ARTIFACT_TOOL, CURRENT_WEATHER_TOOL, WEATHER_FORECAST_TOOL, DRIVING_ROUTE_TOOL, ROUTE_TRAFFIC_TOOL, RESERVATION_TOOL_NAMES, PLACE_SEARCH_TOOL, WALKING_ROUTE_TOOL, TRANSIT_ROUTE_TOOL
from tools.legacy_names import LEGACY_NAMES
from core.tool_result import RAIL_CATALOG, COMPACT_RAIL_CATALOG, CATALOG_VERSIONS, TRAIN_TICKETS_TOOL
from core.tool_result import (HOTEL_CATALOG, COMPACT_HOTEL_CATALOG, HOTEL_STATE_CATALOG, COMPACT_HOTEL_STATE_CATALOG, HOTEL_CATALOGS, RAIL_CATALOGS, COMPACT_CATALOGS)

WEATHER_PERIODS = {'current': CURRENT_WEATHER_TOOL, 'forecast': WEATHER_FORECAST_TOOL}
ROUTE_MODES = {'driving': DRIVING_ROUTE_TOOL, 'walking': WALKING_ROUTE_TOOL, 'transit': TRANSIT_ROUTE_TOOL}
DRAFT_OPERATIONS = {
    'create_from_image': 'create_reservation_draft_from_image', 'refresh': 'refresh_reservation_plan',
    'complete_date': 'complete_reservation_item_date', 'update_item': 'update_reservation_draft_item',
    'update_items': 'update_reservation_draft_items', 'add_item': 'add_reservation_item',
    'set_reminder_times': 'set_reservation_reminder_times',
}
MODIFY_OPERATIONS = {'date': 'modify_reservation_item_date', 'times': 'modify_reservation_item_times'}
CANCEL_TARGETS = {'plan': 'cancel_reservation_plan', 'item': 'cancel_reservation_item'}


def normalize_tool_call(name, arguments, catalog_version=DEFAULT_CATALOG):
    """Resolve a surface call to the original action and parameters before authorization/cache lookup."""
    if catalog_version not in CATALOG_VERSIONS:
        raise ValueError('不支持的工具目录版本。')
    if type(arguments) is not dict:
        raise ValueError('工具参数必须是对象。')
    values = dict(arguments)
    if name == TRAIN_TICKETS_TOOL and catalog_version not in RAIL_CATALOGS:
        raise ValueError('历史目录不包含铁路动作，请重新发起查询。')
    if name=='query_hotel_prices' and catalog_version not in HOTEL_CATALOGS:
        raise ValueError('历史目录不包含酒店查价，请重新发起查询。')
    if name=='query_hotel_prices' and 'proposal_response' in values and catalog_version in {HOTEL_CATALOG,COMPACT_HOTEL_CATALOG}:
        raise ValueError('历史目录不包含日期提议响应，请发起新的酒店查询。')
    if name in TOOLS_BY_NAME:
        return LEGACY_NAMES.get(name,name), values
    elif catalog_version not in COMPACT_CATALOGS:
        raise ValueError('当前工具目录不包含此入口。')
    elif name == 'query_route':
        mode = values.pop('mode', None)
        if not isinstance(mode, str) or mode not in ROUTE_MODES:
            raise ValueError('路线 mode 无效。')
        if 'traffic' in values and (mode != 'driving' or type(values['traffic']) is not bool):
            raise ValueError('traffic 仅适用于驾车，且必须为布尔值。')
        traffic = values.pop('traffic', False)
        action = ROUTE_TRAFFIC_TOOL if traffic else ROUTE_MODES[mode]
    else:
        selectors = {
            'query_weather': ('period', WEATHER_PERIODS),
            'reservation_draft': ('operation', DRAFT_OPERATIONS),
            'modify_reservation_item': ('operation', MODIFY_OPERATIONS),
            'cancel_reservation': ('target_kind', CANCEL_TARGETS),
        }
        if name not in selectors:
            raise ValueError('未知工具入口。')
        selector, actions = selectors[name]
        variant = values.pop(selector, None)
        if not isinstance(variant, str) or variant not in actions:
            raise ValueError('工具操作类型无效。')
        action = actions[variant]
        if name == 'cancel_reservation':
            code = values.pop('target_code', None)
            if values:
                raise ValueError('取消参数包含其他操作的字段。')
            values = {('plan_code' if variant == 'plan' else 'item_code'): code}
    error = validate_arguments(values, TOOLS_BY_NAME[action]['function']['parameters'])
    if error:
        raise ValueError(error)
    return action, values


def _merged_schema(name, description, variants, selector, allowed):
    selected = {key: action for key, action in variants.items() if action in allowed}
    if not selected:
        return None
    properties = {selector: {'type': 'string', 'enum': list(selected)}}
    common = None
    for action in selected.values():
        parameters = TOOLS_BY_NAME[action]['function']['parameters']
        properties.update(deepcopy(parameters.get('properties', {})))
        required = set(parameters.get('required', ()))
        common = required if common is None else common & required
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': [selector, *sorted(common or ())], 'additionalProperties': False}}}


def _catalog_schema(name,version):
    schema=deepcopy(TOOLS_BY_NAME[name])
    if name=='query_hotel_prices' and version in {HOTEL_CATALOG,COMPACT_HOTEL_CATALOG}:
        schema['function']['parameters']['properties'].pop('proposal_response',None)
    return schema


def catalog_schemas(allowed_actions=None, catalog_version=DEFAULT_CATALOG):
    names = tuple(TOOLS_BY_NAME) if allowed_actions is None else tuple(dict.fromkeys(allowed_actions))
    allowed = set(names)
    if catalog_version in (CATALOG_VERSIONS-COMPACT_CATALOGS):
        return [_catalog_schema(name,catalog_version) for name in names if name in TOOLS_BY_NAME
                and (name!=TRAIN_TICKETS_TOOL or catalog_version in RAIL_CATALOGS)
                and (name!='query_hotel_prices' or catalog_version in HOTEL_CATALOGS)]
    if catalog_version not in COMPACT_CATALOGS:
        raise ValueError('Unknown tool catalog version')
    schemas = []
    for name, description, variants, selector in (
        ('query_weather', '查询天气；period 必须区分 current 当前天气和 forecast 未来预报。', WEATHER_PERIODS, 'period'),
        ('reservation_draft', '仅编辑未确认预约草稿；按 operation 使用相应字段，不执行正式确认。', DRAFT_OPERATIONS, 'operation'),
        ('modify_reservation_item', '修改已确认预约的单个项目；date 与 times 使用各自字段。', MODIFY_OPERATIONS, 'operation'),
    ):
        schema = _merged_schema(name, description, variants, selector, allowed)
        if schema:
            schemas.append(schema)
    route_variants = {**ROUTE_MODES, 'traffic': ROUTE_TRAFFIC_TOOL}
    route = _merged_schema('query_route', '查询路线；mode=driving/walking/transit。traffic 仅在驾车时使用，true 表示实时路况；公交必须提供 city。',
                           route_variants, 'mode', allowed)
    if route:
        parameters = route['function']['parameters']
        modes = [mode for mode, action in ROUTE_MODES.items() if action in allowed]
        if ROUTE_TRAFFIC_TOOL in allowed and 'driving' not in modes:
            modes.insert(0, 'driving')
        parameters['properties']['mode']['enum'] = modes
        if DRIVING_ROUTE_TOOL in allowed or ROUTE_TRAFFIC_TOOL in allowed:
            traffic_values = ([False] if DRIVING_ROUTE_TOOL in allowed else []) + ([True] if ROUTE_TRAFFIC_TOOL in allowed else [])
            parameters['properties']['traffic'] = {'type': 'boolean', 'enum': traffic_values}
            if DRIVING_ROUTE_TOOL not in allowed and modes == ['driving']:
                parameters['required'].append('traffic')
        schemas.append(route)
    cancel = _merged_schema('cancel_reservation', '取消明确的计划或单项；target_kind 和 target_code 必须对应用户指定对象。',
                            CANCEL_TARGETS, 'target_kind', allowed)
    if cancel:
        parameters = cancel['function']['parameters']
        parameters['properties'] = {'target_kind': parameters['properties']['target_kind'], 'target_code': {'type': 'string'}}
        parameters['required'] = ['target_kind', 'target_code']
        schemas.append(cancel)
    for name in ('search_travel_places', 'list_reservation_plans', 'confirm_reservation_plan',
                 'request_missing_input', 'read_tool_artifact', 'query_official_website', 'continue_official_websites', TRAIN_TICKETS_TOOL, 'query_hotel_prices'):
        if name in allowed:
            if name==TRAIN_TICKETS_TOOL and catalog_version not in RAIL_CATALOGS:
                continue
            if name=='query_hotel_prices' and catalog_version not in HOTEL_CATALOGS:continue
            schemas.append(_catalog_schema(name,catalog_version))
    return schemas

from core.tool_result import TOOL_BY_INTENT



@dataclass(frozen=True)
class ToolDefinition:
    name: str
    owner: str
    canonical_action: str
    mode: str
    requires_context: bool
    resource_field: str
    handler: ToolExecutor | None
    schema: dict
    parallel_safe: bool = False
    repeatable: bool = False


class ToolRegistry:
    def __init__(self, definitions):
        entries = {}
        for definition in definitions:
            if not callable(definition.handler):
                raise ValueError("A registered tool requires an explicit handler.")
            if definition.name in entries:
                raise ValueError('Duplicate tool owner: ' + definition.name)
            if definition.schema['function']['name'] != definition.name:
                raise ValueError('Tool schema name does not match its registration.')
            if definition.resource_field and definition.resource_field not in definition.schema['function']['parameters']['required']:
                raise ValueError('Resource-bound tools must require their resource field.')
            entries[definition.name] = definition
        self.definitions = MappingProxyType(entries)

    def execute(self, name, arguments, context=None):
        try:
            name, arguments = normalize_tool_call(name, arguments,
                context.catalog_version or DEFAULT_CATALOG if context else DEFAULT_CATALOG)
        except ValueError as error:
            known = name in LEGACY_NAMES or name in {'query_weather', 'query_route', 'reservation_draft', 'modify_reservation_item', 'cancel_reservation'}
            return ToolResult('failed', name, data=str(error), error_code='invalid_argument' if known else 'unknown_tool')
        definition = self.definitions.get(name)
        if definition is None:
            return ToolResult('failed', name, data='未知工具。', error_code='unknown_tool')
        error = validate_arguments(arguments, definition.schema['function']['parameters'])
        if error:
            return ToolResult('failed', definition.canonical_action, data=error, error_code='invalid_argument')
        resource = str(arguments.get(definition.resource_field, '')) if definition.resource_field else ''
        if definition.requires_context and context is None:
            return ToolResult('failed', definition.canonical_action, resource, data='工具缺少用户上下文。', error_code='missing_context')
        if context is not None:
            if context.allowed_actions is not None and definition.canonical_action not in context.allowed_actions:
                return ToolResult('failed', definition.canonical_action, resource, data='本轮未授权该操作。', error_code='operation_denied')
            allowed_resources = {value for action, value in context.action_resources if action == definition.canonical_action}
            if definition.resource_field and allowed_resources and resource not in allowed_resources:
                return ToolResult('failed', definition.canonical_action, resource, data='对象不在本轮操作范围内。', error_code='wrong_resource')
        result = definition.handler(definition.canonical_action, arguments, context)
        if not isinstance(result, ToolResult) or result.action != definition.canonical_action:
            raise TypeError('Registered handlers must return their canonical ToolResult.')
        return result


def request_input(name, arguments, context):
    question = arguments['question'].strip()
    if not 1 <= len(question) <= 500:
        return ToolResult('failed', name, data='请提供 1 到 500 字的必要追问。', error_code='invalid_question')
    return ToolResult('needs_input', name, data=question, missing_fields=tuple(arguments['missing_fields']))


# Public names and canonical operation identities are explicit, never plugin-scanned.
TOOL_LAYOUT = (
    ('query_hotel_prices', 'hotel_prices', 'read', True, ''),
    (TRAIN_TICKETS_TOOL, 'travel_query', 'read', True, ''),
    ('continue_official_websites', 'search', 'read', True, ''),
    ('query_official_website', 'search', 'read', True, ''),
    ('get_current_weather', 'travel_query', 'read', False, ''),
    ('get_weather_forecast', 'travel_query', 'read', False, ''),
    ('get_driving_route', 'travel_query', 'read', False, ''),
    ('get_route_traffic', 'travel_query', 'read', False, ''),
    ('search_travel_places', 'travel_query', 'read', False, ''),
    ('get_walking_route', 'travel_query', 'read', False, ''),
    ('get_transit_route', 'travel_query', 'read', False, ''),
    ('create_reservation_draft_from_image', 'reservation', 'write', True, ''),
    ('list_reservation_plans', 'reservation', 'read', True, ''),
    ('refresh_reservation_plan', 'reservation', 'write', True, 'plan_code'),
    ('confirm_reservation_plan', 'reservation', 'write', True, 'plan_code'),
    ('cancel_reservation_plan', 'reservation', 'write', True, 'plan_code'),
    ('complete_reservation_item_date', 'reservation', 'write', True, 'plan_code'),
    ('update_reservation_draft_item', 'reservation', 'write', True, 'plan_code'),
    ('update_reservation_draft_items', 'reservation', 'write', True, 'plan_code'),
    ('add_reservation_item', 'reservation', 'write', True, 'plan_code'),
    ('set_reservation_reminder_times', 'reservation', 'write', True, 'plan_code'),
    ('modify_reservation_item_date', 'reservation', 'write', True, 'item_code'),
    ('modify_reservation_item_times', 'reservation', 'write', True, 'item_code'),
    ('cancel_reservation_item', 'reservation', 'write', True, 'item_code'),
    ('read_tool_artifact', 'tool_artifacts', 'read', True, 'artifact_id'),
    ('request_missing_input', 'conversation', 'clarify', False, ''),
)
TOOL_DEFINITIONS = MappingProxyType({name: ToolDefinition(
    name, owner, name, mode, context, resource, None, TOOLS_BY_NAME[name],
    parallel_safe=owner == 'travel_query', repeatable=name == 'list_reservation_plans')
    for name, owner, mode, context, resource in TOOL_LAYOUT})


def repeatable_tool(name):
    definition = TOOL_DEFINITIONS.get(name)
    return bool(definition and definition.repeatable)


def build_registry(*, travel_query, reservation, artifact, search=None, hotel_prices=None):
    handlers = {'travel_query': travel_query, 'reservation': reservation,
                'tool_artifacts': artifact, 'conversation': request_input,
                'hotel_prices':hotel_prices or (lambda name,args,context: ToolResult('failed',name,error_code='not_available',data='酒店查价服务暂不可用。')),
                'search': search or (lambda name,args,context: ToolResult('failed',name,error_code='not_available',data='官网搜索暂不可用。'))}
    return ToolRegistry(replace(definition, handler=handlers[definition.owner])
                        for definition in TOOL_DEFINITIONS.values())


class AgentToolRouter:
    def __init__(self, travel_service, reservation_tool, artifact_repository=None, *, delivery_repository=None, search_tool=None):
        self.travel_service = travel_service
        self.reservation_tools = reservation_tool
        self.artifact_repository = artifact_repository
        self.delivery_repository = delivery_repository
        self.registry = build_registry(travel_query=self._read_result, reservation=reservation_tool.execute_result,
                                       artifact=self._artifact_result, search=search_tool.execute_result if search_tool else None,
                                       hotel_prices=self._hotel_result)
        if search_tool is not None:
            search_tool.executor = self.execute_result

    def freeze_catalog(self, context, requested):
        if context is None or self.delivery_repository is None:
            return requested
        return self.delivery_repository.freeze_tool_catalog(context.event_id, requested)

    def execute_result(
            self, name, arguments, context=None) -> ToolResult:
        try:
            result = self.registry.execute(name, arguments, context)
        except Exception as error:
            if self.artifact_repository is not None:
                self.artifact_repository.store.diagnostics.record('tool', {
                    'event_key': context.event_id if context else '', 'name': name,
                    'status': 'interrupted', 'error_type': type(error).__name__})
            raise
        if self.artifact_repository is not None:
            self.artifact_repository.store.diagnostics.record('tool', {
                'event_key': context.event_id if context else '', 'name': name,
                'status': result.status, 'error_code': result.error_code, 'attempts': result.attempts,
                'effect_status': result.effect_status, 'state_version': result.state_version,
                'operation_key': result.operation_key})
        return result

    def _artifact_result(self, name, arguments, context):
        if self.artifact_repository is None:
            return ToolResult('failed', name, data='工具结果存储暂不可用。', error_code='not_available')
        try:
            page = self.artifact_repository.read(context, arguments['artifact_id'],
                arguments.get('cursor', 0), arguments.get('max_chars', 4000))
            return ToolResult('completed', name, page['artifact_id'], data=json.dumps(page, ensure_ascii=False))
        except PermissionError as exc:
            return ToolResult('failed', name, data=str(exc), error_code='permission_denied')
        except (TypeError, ValueError) as exc:
            return ToolResult('failed', name, data=str(exc), error_code='invalid_argument')

    def _hotel_result(self,name,arguments,context):
        if not getattr(self.travel_service,'hotel_prices',None):
            return ToolResult('failed',name,error_code='not_available',data='酒店查价服务暂不可用。')
        return self.travel_service.hotel_prices.execute_result(name,arguments,context)

    def _read_result(self, name, arguments, context):
        result = self.travel_service.execute_scoped_result(name, arguments, context)
        if context is not None and self.artifact_repository is not None and len(result.data) > 6000:
            artifact_id = self.artifact_repository.save(context, name, result.data)
            text = result.data[:4000].rstrip() + (
                f'\n[结果已保存为 {artifact_id}；存储上限500000字符，超出部分未保存。需要更多内容时调用 {READ_TOOL_ARTIFACT_TOOL} 分页读取。]')
            return replace(result, data=text, artifact_id=artifact_id)
        return result

    def execute(self, name, arguments, context=None) -> str:
        """Compatibility rendering; all execution still passes through the registry."""
        result = self.execute_result(name, arguments, context)
        return ('工具错误：' if result.status == 'failed' else '') + result.data.removeprefix('工具错误：')
