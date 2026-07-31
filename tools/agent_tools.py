from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CURRENT_WEATHER_TOOL = "get_current_weather"
WEATHER_FORECAST_TOOL = "get_weather_forecast"
DRIVING_ROUTE_TOOL = "get_driving_route"
ROUTE_TRAFFIC_TOOL = "get_route_traffic"

TRAVEL_TOOL_NAMES = (
    CURRENT_WEATHER_TOOL,
    WEATHER_FORECAST_TOOL,
    DRIVING_ROUTE_TOOL,
    ROUTE_TRAFFIC_TOOL,
)

RESERVATION_TOOL_NAMES = (
    "list_reservation_plans",
    "refresh_reservation_plan",
    "confirm_reservation_plan",
    "cancel_reservation_plan",
    "complete_reservation_item_date",
    "add_reservation_item",
    "set_reservation_reminder_times",
    "modify_reservation_item_date",
    "modify_reservation_item_times",
    "cancel_reservation_item",
)


@dataclass(frozen=True)
class AgentToolContext:
    platform: str
    group_id: str
    creator_id: str
    event_id: str


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
