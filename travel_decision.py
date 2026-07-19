from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from commands import parse_command


Intent = Literal[
    "weather",
    "forecast",
    "route",
    "traffic",
    "document",
    "general",
]


@dataclass(frozen=True)
class TravelDecision:
    intent: Intent
    require_live_data: bool
    allowed_tools: tuple[str, ...]
    needs_clarification: bool
    response_detail: Literal["brief", "normal"]


TOOL_BY_INTENT = {
    "weather": ("get_current_weather",),
    "forecast": ("get_weather_forecast",),
    "route": ("get_driving_route",),
    "traffic": ("get_route_traffic",),
    "document": (),
    "general": (),
}


def decide_travel_action(user_message: str) -> TravelDecision:
    text = " ".join((user_message or "").strip().split())
    command = parse_command(text)
    if command.name in {"weather", "forecast", "route", "traffic"}:
        intent: Intent = command.name
        needs_clarification = bool(command.error)
    elif command.name == "upload_document":
        intent = "document"
        needs_clarification = False
    else:
        intent = _natural_intent(text)
        needs_clarification = (
            intent in {"route", "traffic"}
            and not _has_route_endpoints(text)
        )

    return TravelDecision(
        intent=intent,
        require_live_data=intent in {
            "weather", "forecast", "route", "traffic"
        },
        allowed_tools=TOOL_BY_INTENT[intent],
        needs_clarification=needs_clarification,
        response_detail=(
            "brief" if intent in {"weather", "forecast"} else "normal"
        ),
    )


def _natural_intent(text: str) -> Intent:
    if _contains(text, "路况", "拥堵", "堵车", "车流", "通行"):
        return "traffic"
    if _contains(text, "路线", "怎么走", "距离", "驾车", "开车", "耗时"):
        return "route"
    if _contains(text, "天气预报", "预报", "明天", "后天", "未来"):
        return "forecast"
    if _contains(text, "天气", "气温", "下雨", "降雨", "下雪", "大风"):
        return "weather"
    if _contains(text, "文档", "行程单", "计划书", "资料", "住宿安排"):
        return "document"
    return "general"


def _contains(text: str, *signals: str) -> bool:
    return any(signal in text for signal in signals)


def _has_route_endpoints(text: str) -> bool:
    parts = re.split(r"\s*(?:->|→|到|至)\s*", text, maxsplit=1)
    return len(parts) == 2 and all(part.strip() for part in parts)
