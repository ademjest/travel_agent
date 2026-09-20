from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from tools.agent_tools import (
    CREATE_RESERVATION_DRAFT_TOOL,
    CURRENT_WEATHER_TOOL,
    DRIVING_ROUTE_TOOL,
    RESERVATION_TOOL_NAMES,
    ROUTE_TRAFFIC_TOOL,
    UPDATE_RESERVATION_DRAFT_ITEMS_TOOL,
    WEATHER_FORECAST_TOOL,
    PLACE_SEARCH_TOOL,
    WALKING_ROUTE_TOOL,
    TRANSIT_ROUTE_TOOL,
)
from core.commands import ITEM_CODE, PLAN_CODE, parse_command


Intent = Literal[
    "weather",
    "forecast",
    "route",
    "traffic",
    "reservation",
    "document",
    "general",
    "places",
    "walking",
    "transit",
]


@dataclass(frozen=True)
class TravelDecision:
    intent: Intent
    intents: tuple[Intent, ...]
    require_live_data: bool
    allowed_tools: tuple[str, ...]
    required_tool_groups: tuple[tuple[str, ...], ...]
    needs_clarification: bool
    response_detail: Literal["brief", "normal"]
    action_resources: tuple[tuple[str, str], ...] = ()
    clarification_reply: str = ""


TOOL_BY_INTENT = {
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

INTENT_ORDER: tuple[Intent, ...] = (
    "reservation",
    "traffic",
    "walking",
    "transit",
    "route",
    "forecast",
    "weather",
    "document",
    "places",
    "general",
)

_RESERVATION_IMAGE_SIGNALS = (
    "制定预约",
    "根据图片制定预约",
    "按这张攻略",
)
_RESERVATION_DRAFT_EDIT_SIGNALS = (
    "无需预约",
    "不去",
    "不参观",
    "日期补为",
    "日期改为",
    "修改日期",
    "补充日期",
)


def decide_travel_action(user_message: str, *, has_document_context: bool = False) -> TravelDecision:
    text = " ".join((user_message or "").strip().split())
    command = parse_command(text)
    intents = _command_intents(command.name) or _natural_intents(text)
    if (has_document_context and 'places' in intents
            and _contains(text, '我们', '安排', '订了', '之前', '行程')
            and not _contains(text, '推荐', '找一家', '找一下', '搜索', '附近')):
        intents = tuple(intent for intent in intents if intent != 'places') + ('document',)
    if not intents:
        intents = ("general",)

    action_text = "，".join(
        clause for clause in re.split(r"[，,；;。]", text)
        if not re.search(r"不要|别|不用|不必|禁止|不允许|不能", clause)
    )
    draft_edit = (
        "reservation" in intents
        and _contains(action_text, *_RESERVATION_DRAFT_EDIT_SIGNALS)
    )
    reservation_actions = []
    action_signals = (
        (("确认",), "confirm_reservation_plan"),
        (("取消",), "cancel_reservation_item" if re.search(ITEM_CODE, text) else "cancel_reservation_plan"),
        (("刷新",), "refresh_reservation_plan"),
        (("补充预约",), "complete_reservation_item_date"),
        (("新增预约",), "add_reservation_item"),
        (("设置提醒",), "set_reservation_reminder_times"),
        (("修改预约",), "modify_reservation_item_times" if "时间" in text else "modify_reservation_item_date"),
        (_RESERVATION_IMAGE_SIGNALS, CREATE_RESERVATION_DRAFT_TOOL),
    )
    for signals, tool in action_signals:
        if _contains(action_text, *signals):
            reservation_actions.append(tool)
    if draft_edit:
        reservation_actions = [UPDATE_RESERVATION_DRAFT_ITEMS_TOOL]
    resources = tuple(re.findall(f"{PLAN_CODE}|{ITEM_CODE}", action_text))
    action_resources = []
    ambiguous_actions = False
    for clause in action_text.split("，"):
        clause_resources = tuple(re.findall(f"{PLAN_CODE}|{ITEM_CODE}", clause))
        clause_tools = [tool for signals, tool in action_signals if _contains(clause, *signals)]
        if len(set(clause_tools)) > 1 and len(clause_resources) > 1:
            ambiguous_actions = True
        if not draft_edit:
            action_resources.extend((tool, resource) for tool in clause_tools
                                    for resource in clause_resources or resources)
    if draft_edit:
        action_resources = [(UPDATE_RESERVATION_DRAFT_ITEMS_TOOL, resource) for resource in resources]
    reservation_tools = tuple(dict.fromkeys(("list_reservation_plans", *reservation_actions)))
    allowed_tools = tuple(dict.fromkeys(
        tool
        for intent in intents
        for tool in (
            (UPDATE_RESERVATION_DRAFT_ITEMS_TOOL,)
            if intent == "reservation" and draft_edit
            else reservation_tools if intent == "reservation"
            else TOOL_BY_INTENT[intent]
        )
    ))
    required_groups = []
    for intent in intents:
        if intent in {"weather", "forecast", "route", "traffic", "places", "walking", "transit"}:
            required_groups.append(TOOL_BY_INTENT[intent])
    if "reservation" in intents:
        if _contains(action_text, *_RESERVATION_IMAGE_SIGNALS):
            required_groups.append((CREATE_RESERVATION_DRAFT_TOOL,))
        elif draft_edit:
            required_groups.append((UPDATE_RESERVATION_DRAFT_ITEMS_TOOL,))
        else:
            required_groups.extend((tool,) for tool in reservation_actions or ("list_reservation_plans",))

    route_intents = set(intents) & {"route", "traffic", "walking", "transit"}
    needs_clarification = (
        bool(route_intents)
        and not _has_route_endpoints(text)
        and set(intents) <= {"route", "traffic", "walking", "transit"}
    )
    primary = next(
        (intent for intent in INTENT_ORDER if intent in intents),
        "general",
    )
    return TravelDecision(
        intent=primary,
        intents=intents,
        require_live_data=any(
            intent in {"weather", "forecast", "route", "traffic", "places", "walking", "transit"}
            for intent in intents
        ),
        allowed_tools=allowed_tools,
        required_tool_groups=tuple(required_groups),
        needs_clarification=needs_clarification or ambiguous_actions,
        action_resources=tuple(dict.fromkeys(action_resources)),
        clarification_reply=("请用逗号分开每项操作，并分别写明计划或项目编号。" if ambiguous_actions
                             else "请告诉我出行起点和终点，以及所在城市。" if needs_clarification and set(intents) & {'transit', 'walking'} else ""),
        response_detail=(
            "brief"
            if set(intents) <= {"weather", "forecast"}
            else "normal"
        ),
    )


def _command_intents(command_name: str) -> tuple[Intent, ...]:
    if command_name in {"weather", "forecast", "route", "traffic"}:
        return (command_name,)
    if command_name == "upload_document":
        return ("document",)
    if command_name.startswith("reservation_"):
        return ("reservation",)
    return ()


def _natural_intents(text: str) -> tuple[Intent, ...]:
    detected: list[Intent] = []
    if (
            _contains(text, "预约", "提醒", "景点票", "门票")
            or re.search(PLAN_CODE, text) or re.search(ITEM_CODE, text)):
        detected.append("reservation")

    traffic = _contains(text, "路况", "拥堵", "堵车", "车流", "通行")
    route = _contains(text, "路线", "怎么走", "距离", "驾车", "开车", "耗时")
    combined_driving_risk = _contains(text, "适合自驾", "自驾风险", "出行风险")
    if _contains(text, '公交', '地铁', '公共交通'):
        detected.append('transit')
    elif _contains(text, '步行', '走路'):
        detected.append('walking')
    elif traffic or combined_driving_risk:
        detected.append("traffic")
    elif route:
        detected.append("route")

    future = _contains(text, "天气预报", "预报", "明天", "后天", "未来")
    current = _contains(text, "现在天气", "当前天气", "实时天气")
    weather = _contains(text, "天气", "气温", "下雨", "降雨", "下雪", "大风")
    if future and (weather or _contains(text, "预报")):
        detected.append("forecast")
    if current or (weather and not future):
        detected.append("weather")

    if _contains(
            text,
            "文档",
            "文件",
            "行程单",
            "计划书",
            "资料",
            "住宿安排",
            "表格",
            "xlsx",
            "excel",
            "docx"):
        detected.append("document")
    if ('document' not in detected and 'reservation' not in detected
            and _contains(text, '酒店', '餐馆', '餐厅', '博物馆', '景点推荐', '推荐景点', '住哪里', '吃什么', '有什么好玩')
            and not set(detected) & {'route', 'traffic', 'walking', 'transit'}
            and (not set(detected) & {'weather', 'forecast'} or _contains(text, '推荐', '找酒店', '找餐馆'))):
        detected.append('places')
    return tuple(dict.fromkeys(detected))


def _contains(text: str, *signals: str) -> bool:
    return any(signal in text for signal in signals)


def _has_route_endpoints(text: str) -> bool:
    parts = re.split(r"\s*(?:->|→|到|至)\s*", text, maxsplit=1)
    return len(parts) == 2 and all(part.strip() for part in parts)
