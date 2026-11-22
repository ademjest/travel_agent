from dataclasses import asdict, dataclass
import json
from typing import Any, Callable

from core.chat_transport import ChatAttachment, ChatEvent
from core.rail_contract import SEAT_CLASSES, SEAT_REQUIREMENTS, PRICE_PREFERENCES
from core.hotel_contract import SCHEMA as HOTEL_PRICE_SCHEMA

LEGACY_CATALOG = 'legacy-v1'
COMPACT_CATALOG = 'compact-v1'
RAIL_CATALOG = 'legacy-v2'
COMPACT_RAIL_CATALOG = 'compact-v2'
HOTEL_CATALOG = 'legacy-v3'
COMPACT_HOTEL_CATALOG = 'compact-v3'
HOTEL_STATE_CATALOG='legacy-v4'
COMPACT_HOTEL_STATE_CATALOG='compact-v4'
HOTEL_CATALOGS={HOTEL_CATALOG,COMPACT_HOTEL_CATALOG,HOTEL_STATE_CATALOG,COMPACT_HOTEL_STATE_CATALOG}
RAIL_CATALOGS={RAIL_CATALOG,COMPACT_RAIL_CATALOG,*HOTEL_CATALOGS}
COMPACT_CATALOGS={COMPACT_CATALOG,COMPACT_RAIL_CATALOG,COMPACT_HOTEL_CATALOG,COMPACT_HOTEL_STATE_CATALOG}
CATALOG_VERSIONS={LEGACY_CATALOG,*RAIL_CATALOGS,*COMPACT_CATALOGS}
DEFAULT_CATALOG=HOTEL_STATE_CATALOG


@dataclass(frozen=True)
class AgentToolContext:
    platform: str
    group_id: str
    creator_id: str
    event_id: str
    attachments: tuple[ChatAttachment, ...] = ()
    allowed_actions: frozenset[str] | None = None
    action_resources: tuple[tuple[str, str], ...] = ()
    catalog_version: str = ''
    event: ChatEvent | None = None
    execution_data: dict | None = None
    available: Callable[[], bool] | None = None


class ReadFailed(RuntimeError):
    def __init__(self, result):
        super().__init__(result.error_code or result.status)
        self.retryable = result.status == 'failed' and result.retryable
        self.retry_after_seconds = result.retry_after_seconds
        if self.retry_after_seconds is not None and self.retry_after_seconds > 86400:
            self.retryable = False
        if not self.retryable:
            self.retry_after_seconds = None


@dataclass(frozen=True)
class ToolResult:
    status: str
    action: str
    resource_id: str = ""
    state_version: int | None = None
    data: str = ""
    error_code: str = ""
    retryable: bool = False
    retry_after_seconds: int | None = None
    effect_status: str = 'none'
    operation_key: str = ''
    artifact_id: str = ''
    attempts: int = 1
    missing_fields: tuple[str, ...] = ()
    payload: dict | None = None

    def __post_init__(self):
        object.__setattr__(self, 'missing_fields', tuple(self.missing_fields))

    @property
    def canonical_action(self):
        return self.action

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_text(cls, action: str, text: str, resource_id: str = ""):
        """Legacy receipt import only; production executors must set state directly."""
        failed = text.startswith("工具错误：")
        error_code, retryable, retry_after = '', False, None
        if failed:
            normalized = text.lower()
            if any(value in normalized for value in ('429', 'rate limit', '频率', '限流')):
                error_code, retryable, retry_after = 'rate_limit', True, 30
            elif any(value in normalized for value in ('超时', 'timeout', '网络', '连接', '暂时', '稍后', '繁忙', '5xx')):
                error_code, retryable, retry_after = 'temporary_upstream', True, 5
            elif any(value in normalized for value in ('权限', '不属于', '无权')):
                error_code = 'permission_denied'
            elif any(value in normalized for value in ('缺少', '参数', '格式')):
                error_code = 'invalid_argument'
            elif any(value in normalized for value in ('未配置', '未知工具', '不支持')):
                error_code = 'not_available'
            else:
                error_code = 'tool_error'
        return cls(
            status="failed" if failed else "completed", action=action,
            resource_id=resource_id, data=text,
            error_code=error_code, retryable=retryable, retry_after_seconds=retry_after,
        )


ToolExecutor = Callable[[str, dict[str, object], AgentToolContext | None], ToolResult]


def validate_arguments(value, schema: dict, path: str = "参数") -> str:
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    expected = schema.get("type")
    if expected in types and type(value) is not types[expected]:
        return f"{path} 必须是 {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} 不在允许值中"
    if expected == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", ())):
            return f"{path} 缺少必要字段"
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return f"{path} 包含未定义字段"
        for key, item in value.items():
            error = validate_arguments(item, properties.get(key, {}), f"{path}.{key}")
            if error:
                return error
    if expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            return f"{path} 数量超出范围"
        for item in value:
            error = validate_arguments(item, schema.get("items", {}), path)
            if error:
                return error
    if expected == "integer" and value < schema.get("minimum", value):
        return f"{path} 小于允许值"
    if expected == "integer" and value > schema.get("maximum", value):
        return f"{path} 大于允许值"
    return ""


CURRENT_WEATHER_TOOL = "get_current_weather"


WEATHER_FORECAST_TOOL = "get_weather_forecast"


DRIVING_ROUTE_TOOL = "get_driving_route"


ROUTE_TRAFFIC_TOOL = "get_route_traffic"


PLACE_SEARCH_TOOL = 'search_travel_places'


WALKING_ROUTE_TOOL = 'get_walking_route'


TRANSIT_ROUTE_TOOL = 'get_transit_route'
TRAIN_TICKETS_TOOL = 'query_train_tickets'


ASK_USER_TOOL = 'request_missing_input'


READ_TOOL_ARTIFACT_TOOL = 'read_tool_artifact'
OFFICIAL_WEBSITE_TOOL = 'query_official_website'


CREATE_RESERVATION_DRAFT_TOOL = "create_reservation_draft_from_image"


UPDATE_RESERVATION_DRAFT_ITEM_TOOL = "update_reservation_draft_item"


UPDATE_RESERVATION_DRAFT_ITEMS_TOOL = "update_reservation_draft_items"


TRAVEL_TOOL_NAMES = (
    'query_hotel_prices',
    CURRENT_WEATHER_TOOL,
    WEATHER_FORECAST_TOOL,
    DRIVING_ROUTE_TOOL,
    ROUTE_TRAFFIC_TOOL,
    PLACE_SEARCH_TOOL,
    WALKING_ROUTE_TOOL,
    TRANSIT_ROUTE_TOOL,
    TRAIN_TICKETS_TOOL,
)


RESERVATION_TOOL_NAMES = (
    CREATE_RESERVATION_DRAFT_TOOL,
    "list_reservation_plans",
    "refresh_reservation_plan",
    "confirm_reservation_plan",
    "cancel_reservation_plan",
    "complete_reservation_item_date",
    UPDATE_RESERVATION_DRAFT_ITEM_TOOL,
    UPDATE_RESERVATION_DRAFT_ITEMS_TOOL,
    "add_reservation_item",
    "set_reservation_reminder_times",
    "modify_reservation_item_date",
    "modify_reservation_item_times",
    "cancel_reservation_item",
)


def _function_tool(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


STRING = {"type": "string"}


PLAN_CODE = {
    "type": "string",
    "description": "预约计划编号，例如 R-20260722-001。",
}


ITEM_CODE = {
    "type": "string",
    "description": "预约项目编号，例如 A-000123。",
}


ISO_DATE = {
    "type": "string",
    "description": "ISO 日期，格式 YYYY-MM-DD。",
}


REMINDER_TIMES = {
    "type": "array",
    "items": {
        "type": "string",
        "description": "北京时间，格式 YYYY-MM-DD HH:MM。",
    },
    "minItems": 1,
    "maxItems": 8,
}


TOOLS = [
    _function_tool('query_hotel_prices',
        '只读查询酒店参考房价，无需行程或预算。lookup核对当前价格、refresh明确重查、saved仅看历史/原文。'
        'targets为具体门店和城市；或discovery为城市区域与品牌。stay收集入住退房日期原话、房数、成人数、儿童年龄（无儿童为[]）。'
        '允许缺项后集中追问，不猜人数，不预订、不保存行程。reference只表达用户引用，不填资源ID。',
        HOTEL_PRICE_SCHEMA['properties'],()),
    _function_tool(TRAIN_TICKETS_TOOL,
        '只读查询单向铁路车票，不创建行程或购票；起终点与明确出发日期必需，日期保留用户原文由服务端换算。',
        {'origin': STRING, 'destination': STRING,
         'travel_date': {'type':'string','description':'明确出发日期的原文，如明天或2026-10-30；未知时必须追问。'},
         'origin_scope': {'type':'string','enum':['city','station']},
         'destination_scope': {'type':'string','enum':['city','station']},
         'train_preference': {'type':'string','enum':['high_speed','high_speed_only','d_train','conventional','all']},
         'departure_window': {'type':'string','description':'用户明确的出发时段原文；如上午、18:00至22:00。'},
         'seat_class': {'type':'string','enum':list(SEAT_CLASSES)},
         'seat_requirement': {'type':'string','enum':sorted(SEAT_REQUIREMENTS)},
         'price_preference': {'type':'string','enum':sorted(PRICE_PREFERENCES)},
         'limit': {'type':'integer','minimum':1,'maximum':10}},
        ('origin','destination','travel_date')),
    _function_tool('continue_official_websites',
        '按当前会话最近一次研究清单继续查询尚未确认的景点官网；不重查已有有效链接。', {}, ()),
    _function_tool(OFFICIAL_WEBSITE_TOOL,
        '独立查找并核对景点官方网站。name 保留分馆/园区；city 仅在用户明确给出时填写。无需行程或出行日期。',
        {'name':STRING, 'city':STRING}, ('name',)),
    _function_tool(READ_TOOL_ARTIFACT_TOOL,
        '按编号分页读取本次会话中已保存的大型工具结果。不得猜测其他会话的编号。',
        {'artifact_id': {'type': 'string', 'description': 'TA- 开头的工具结果编号'},
         'cursor': {'type': 'integer', 'minimum': 0},
         'max_chars': {'type': 'integer', 'minimum': 500}},
        ('artifact_id',)),
    _function_tool(ASK_USER_TOOL,
        '缺少起终点等必要信息时结束本轮并追问。公共交通两端已给出时，先用路线工具核验所属城市；只有工具无法确认或城市冲突时再追问。只询问缺失字段，不能声称业务已完成。',
        {'question': {'type': 'string'}, 'missing_fields': {'type': 'array', 'minItems': 1, 'maxItems': 4,
            'items': {'type': 'string', 'enum': ['location', 'city', 'origin', 'destination']}}},
        ('question', 'missing_fields')),
    _function_tool(PLACE_SEARCH_TOOL,
        '在明确城市中查询景点、博物馆、餐馆或酒店地点。只返回地点资料，不提供实时房价、余票或库存。',
        {'city': {'type': 'string', 'description': '用户明确的目的地城市'},
         'keywords': {'type': 'string', 'description': '要查找的场所名或类别，例如博物馆、酒店'}},
        ('city', 'keywords')),
    _function_tool(WALKING_ROUTE_TOOL, '查询明确起终点之间的步行路线和预计耗时。',
        {'origin': STRING, 'destination': STRING}, ('origin', 'destination')),
    _function_tool(TRANSIT_ROUTE_TOOL, '查询同一城市内的公交、地铁和步行换乘；城市可省略，由后端定位两端核验，真实地点歧义时才追问。',
        {'origin': STRING, 'destination': STRING, 'city': STRING}, ('origin', 'destination')),
    _function_tool(
        CURRENT_WEATHER_TOOL,
        "查询一个中国地点当前的行政区级实时天气。",
        {
            "location": {
                "type": "string",
                "description": "完整地点名称，例如西宁或青海湖二郎剑景区。",
            }
        },
        ("location",),
    ),
    _function_tool(
        WEATHER_FORECAST_TOOL,
        "查询一个中国地点未来数天的行政区级天气预报。",
        {
            "location": {
                "type": "string",
                "description": "完整地点名称，例如青海湖或茶卡盐湖。",
            }
        },
        ("location",),
    ),
    _function_tool(
        DRIVING_ROUTE_TOOL,
        "查询两地之间的高德推荐驾车路线、距离、预计耗时和收费。",
        {
            "origin": {"type": "string", "description": "驾车起点。"},
            "destination": {"type": "string", "description": "驾车终点。"},
        },
        ("origin", "destination"),
    ),
    _function_tool(
        ROUTE_TRAFFIC_TOOL,
        (
            "查询两地之间的实时交通感知预计耗时、分段路况和拥堵风险。"
            "该工具已经包含路线距离和耗时，一般不需要重复调用驾车路线工具。"
        ),
        {
            "origin": {"type": "string", "description": "驾车起点。"},
            "destination": {"type": "string", "description": "驾车终点。"},
        },
        ("origin", "destination"),
    ),
    _function_tool(
        CREATE_RESERVATION_DRAFT_TOOL,
        (
            "使用当前消息中的一张预约攻略图片创建预约草稿。"
            "只能选择当前事件的附件，不能传入外部 URL。"
        ),
        {
            "attachment_index": {
                "type": "integer",
                "minimum": 1,
                "description": "当前消息中预约图片的序号；只有一张时填写 1。",
            }
        },
        ("attachment_index",),
    ),
    _function_tool(
        "list_reservation_plans",
        "查看当前群中由当前用户创建的预约草稿、已确认计划和项目编号。",
        {},
    ),
    _function_tool(
        "refresh_reservation_plan",
        "用当前群最新旅行文档刷新一个未确认预约草稿中的未决日期。",
        {"plan_code": PLAN_CODE},
        ("plan_code",),
    ),
    _function_tool(
        "confirm_reservation_plan",
        "确认一个预约草稿并创建其中已经就绪的预约提醒。",
        {"plan_code": PLAN_CODE},
        ("plan_code",),
    ),
    _function_tool(
        "cancel_reservation_plan",
        "取消当前用户创建的预约草稿或已确认预约计划。",
        {"plan_code": PLAN_CODE},
        ("plan_code",),
    ),
    _function_tool(
        "complete_reservation_item_date",
        "为预约草稿中的一个项目补充明确游览日期。",
        {
            "plan_code": PLAN_CODE,
            "item_index": {"type": "integer", "minimum": 1},
            "visit_date": ISO_DATE,
        },
        ("plan_code", "item_index", "visit_date"),
    ),
    _function_tool(
        UPDATE_RESERVATION_DRAFT_ITEM_TOOL,
        (
            "修改未确认预约草稿中的一个景点项目。可修改游览日期、"
            "是否需要预约，或把景点标记为不去参观。"
        ),
        {
            "plan_code": PLAN_CODE,
            "item_index": {"type": "integer", "minimum": 1},
            "visit_date": ISO_DATE,
            "requires_reservation": {"type": "boolean"},
            "visit_status": {
                "type": "string",
                "enum": ["visit", "skip"],
                "description": "visit 表示参观；skip 表示不去参观。",
            },
        },
        ("plan_code", "item_index"),
    ),
    _function_tool(
        UPDATE_RESERVATION_DRAFT_ITEMS_TOOL,
        (
            "一次修改未确认预约草稿中的一个或多个景点。直接使用用户提供的"
            "景点名称，不需要先调用列表工具查询项目序号。所有修改会整体成功或整体失败。"
        ),
        {
            "plan_code": PLAN_CODE,
            "updates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "attraction_name": {
                            "type": "string",
                            "description": "草稿中已有的景点名称。",
                        },
                        "visit_date": ISO_DATE,
                        "requires_reservation": {"type": "boolean"},
                        "visit_status": {
                            "type": "string",
                            "enum": ["visit", "skip"],
                            "description": "skip 表示不去参观。",
                        },
                    },
                    "required": ["attraction_name"],
                    "additionalProperties": False,
                },
            },
        },
        ("plan_code", "updates"),
    ),
    _function_tool(
        "add_reservation_item",
        "向现有未确认预约草稿中新增一个景点项目。",
        {
            "plan_code": PLAN_CODE,
            "attraction_name": {
                "type": "string",
                "description": "景点名称。",
            },
            "visit_date": ISO_DATE,
            "requires_reservation": {"type": "boolean"},
            "advance_value": {
                "type": "integer",
                "minimum": 0,
                "maximum": 365,
            },
            "advance_unit": {
                "type": "string",
                "enum": ["day", "month", "none"],
            },
        },
        (
            "plan_code",
            "attraction_name",
            "visit_date",
            "requires_reservation",
            "advance_value",
            "advance_unit",
        ),
    ),
    _function_tool(
        "set_reservation_reminder_times",
        "为未确认草稿中的项目设置一个或多个完整提醒时间，替换默认双提醒。",
        {
            "plan_code": PLAN_CODE,
            "item_index": {"type": "integer", "minimum": 1},
            "times": REMINDER_TIMES,
        },
        ("plan_code", "item_index", "times"),
    ),
    _function_tool(
        "modify_reservation_item_date",
        "修改一个已确认预约项目的游览日期，并重建尚未发送的提醒。",
        {"item_code": ITEM_CODE, "visit_date": ISO_DATE},
        ("item_code", "visit_date"),
    ),
    _function_tool(
        "modify_reservation_item_times",
        "修改一个已确认预约项目的提醒时间。",
        {"item_code": ITEM_CODE, "times": REMINDER_TIMES},
        ("item_code", "times"),
    ),
    _function_tool(
        "cancel_reservation_item",
        "取消一个已确认预约项目及其尚未发送的提醒。",
        {"item_code": ITEM_CODE},
        ("item_code",),
    ),
]


TOOLS_BY_NAME = {
    tool["function"]["name"]: tool
    for tool in TOOLS
}


TOOL_BY_INTENT = {
    'hotel': ('query_hotel_prices',),
    'rail': (TRAIN_TICKETS_TOOL,),
    "weather": (CURRENT_WEATHER_TOOL,),
    "forecast": (WEATHER_FORECAST_TOOL,),
    "route": (DRIVING_ROUTE_TOOL,),
    "traffic": (ROUTE_TRAFFIC_TOOL,),
    "reservation": RESERVATION_TOOL_NAMES,
    "document": (),
    "general": (),
    "places": (PLACE_SEARCH_TOOL,),
    "walking": (WALKING_ROUTE_TOOL,),
    "transit": (TRANSIT_ROUTE_TOOL,),
}

