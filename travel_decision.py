from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agent_tools import (
    CURRENT_WEATHER_TOOL,
    DRIVING_ROUTE_TOOL,
    RESERVATION_TOOL_NAMES,
    ROUTE_TRAFFIC_TOOL,
    WEATHER_FORECAST_TOOL,
)
from commands import parse_command


Intent = Literal[
    "weather",
    "forecast",
    "route",
    "traffic",
    "reservation",
    "document",
    "general",
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


TOOL_BY_INTENT = {
    "weather": (CURRENT_WEATHER_TOOL,),
    "forecast": (WEATHER_FORECAST_TOOL,),
    "route": (DRIVING_ROUTE_TOOL,),
    "traffic": (ROUTE_TRAFFIC_TOOL,),
    "reservation": RESERVATION_TOOL_NAMES,
    "document": (),
    "general": (),
}

INTENT_ORDER: tuple[Intent, ...] = (
    "reservation",
    "traffic",
    "route",
    "forecast",
    "weather",
    "document",
    "general",
)

_RESERVATION_ACTION_SIGNALS = (
    "创建预约",
    "新增预约",
    "预约提醒",
    "确认预约",
    "取消预约",
    "刷新预约",
    "修改预约",
    "设置提醒",
    "补充预约",
    "查看预约",
)


def decide_travel_action(user_message: str) -> TravelDecision:
    text = " ".join((user_message or "").strip().split())
    command = parse_command(text)
    intents = _command_intents(command.name) or _natural_intents(text)
    if not intents:
        intents = ("general",)

    allowed_tools = tuple(dict.fromkeys(
        tool
        for intent in intents
        for tool in TOOL_BY_INTENT[intent]
    ))
    required_groups = []
    for intent in intents:
        if intent in {"weather", "forecast", "route", "traffic"}:
            required_groups.append(TOOL_BY_INTENT[intent])
    if "reservation" in intents and _contains(text, *_RESERVATION_ACTION_SIGNALS):
        required_groups.append(RESERVATION_TOOL_NAMES)

    route_intents = set(intents) & {"route", "traffic"}
    needs_clarification = (
        bool(route_intents)
        and not _has_route_endpoints(text)
        and set(intents) <= {"route", "traffic"}
    )
    primary = next(
        (intent for intent in INTENT_ORDER if intent in intents),
        "general",
    )
    return TravelDecision(
        intent=primary,
        intents=intents,
        require_live_data=any(
            intent in {"weather", "forecast", "route", "traffic"}
            for intent in intents
        ),
        allowed_tools=allowed_tools,
        required_tool_groups=tuple(required_groups),
        needs_clarification=needs_clarification,
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
    if _contains(text, "预约", "提醒", "景点票", "门票"):
        detected.append("reservation")

    traffic = _contains(text, "路况", "拥堵", "堵车", "车流", "通行")
    route = _contains(text, "路线", "怎么走", "距离", "驾车", "开车", "耗时")
    combined_driving_risk = _contains(text, "适合自驾", "自驾风险", "出行风险")
    if traffic or combined_driving_risk:
        detected.append("traffic")
    elif route:
        detected.append("route")

    future = _contains(text, "天气预报", "预报", "明天", "后天", "未来")
    current = _contains(text, "现在天气", "当前天气", "实时天气")
    weather = _contains(text, "天气", "气温", "下雨", "降雨", "下雪", "大风")
    if future:
        detected.append("forecast")
    if current or (weather and not future):
        detected.append("weather")

    if _contains(text, "文档", "行程单", "计划书", "资料", "住宿安排"):
        detected.append("document")
    return tuple(dict.fromkeys(detected))


def _contains(text: str, *signals: str) -> bool:
    return any(signal in text for signal in signals)


def _has_route_endpoints(text: str) -> bool:
    parts = re.split(r"\s*(?:->|→|到|至)\s*", text, maxsplit=1)
    return len(parts) == 2 and all(part.strip() for part in parts)
