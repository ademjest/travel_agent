from __future__ import annotations

from datetime import date
from typing import Mapping

from agent_tools import AgentToolContext, RESERVATION_TOOL_NAMES
from event_idempotency import event_operation_key
from reservation_service import (
    ReservationService,
    parse_beijing_datetime_list,
)


MAX_TOOL_TEXT_CHARS = 500


def _text(arguments: Mapping[str, object], name: str) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise ValueError(f"缺少参数 {name}")
    if len(value) > MAX_TOOL_TEXT_CHARS:
        raise ValueError(f"参数 {name} 过长")
    return value


def _positive_int(arguments: Mapping[str, object], name: str) -> int:
    try:
        value = int(arguments.get(name))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 必须是整数") from exc
    if value < 1:
        raise ValueError(f"参数 {name} 必须大于等于 1")
    return value


def _times(arguments: Mapping[str, object]):
    raw = arguments.get("times")
    if not isinstance(raw, list) or not raw or len(raw) > 8:
        raise ValueError("times 必须包含 1 到 8 个完整北京时间")
    return parse_beijing_datetime_list(
        ",".join(str(value).strip() for value in raw)
    )


class ReservationToolExecutor:
    def __init__(self, service: ReservationService):
        self.service = service

    def execute(
            self,
            name: str,
            arguments: Mapping[str, object],
            context: AgentToolContext) -> str:
        if name not in RESERVATION_TOOL_NAMES:
            raise ValueError(f"未知预约工具 {name}")

        normalized_arguments = dict(arguments)
        operation_key = event_operation_key(
            context.event_id,
            name,
            normalized_arguments,
        )
        cached = self.service.store.get_event_tool_result(operation_key)
        if cached is not None:
            return cached

        result = self._execute_uncached(
            name,
            normalized_arguments,
            context,
            operation_key,
        )
        return self.service.store.save_event_tool_result(
            operation_key,
            context.event_id,
            name,
            normalized_arguments,
            result,
        )

    def _execute_uncached(
            self,
            name: str,
            arguments: Mapping[str, object],
            context: AgentToolContext,
            operation_key: str) -> str:

        platform = context.platform
        group_id = context.group_id
        creator_id = context.creator_id

        if name == "list_reservation_plans":
            return self.service.format_plan_list(
                self.service.list_plans(platform, group_id, creator_id)
            )
        if name == "refresh_reservation_plan":
            result = self.service.refresh_plan(
                platform, group_id, creator_id, _text(arguments, "plan_code")
            )
            prefix = (
                f"已按最新行程刷新 {result.updated_count} 个项目。\n"
                if result.updated_count
                else "未找到可自动补齐的新日期。\n"
            )
            return prefix + self.service.format_draft(result.plan)
        if name == "confirm_reservation_plan":
            plan = self.service.confirm_plan(
                platform, group_id, creator_id, _text(arguments, "plan_code")
            )
            return f"预约计划 {plan.plan_code} 已确认。"
        if name == "cancel_reservation_plan":
            warning = self.service.cancel_plan(
                platform, group_id, creator_id, _text(arguments, "plan_code")
            )
            return (
                "预约计划已取消。"
                + (" 提醒正在发送，可能已经发出。" if warning else "")
            )
        if name == "complete_reservation_item_date":
            plan = self.service.complete_item_date(
                platform,
                group_id,
                creator_id,
                _text(arguments, "plan_code"),
                _positive_int(arguments, "item_index"),
                date.fromisoformat(_text(arguments, "visit_date")),
            )
            return self.service.format_draft(plan)
        if name == "add_reservation_item":
            requires = arguments.get("requires_reservation")
            if not isinstance(requires, bool):
                raise ValueError("requires_reservation 必须是布尔值")
            unit = _text(arguments, "advance_unit")
            if unit not in {"day", "month", "none"}:
                raise ValueError("advance_unit 必须是 day、month 或 none")
            try:
                advance_value = int(arguments.get("advance_value", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("advance_value 必须是整数") from exc
            if not 0 <= advance_value <= 365:
                raise ValueError("advance_value 必须在 0 到 365 之间")
            if requires and (unit == "none" or advance_value < 1):
                raise ValueError("需要预约时必须提供有效提前天数或月数")
            if not requires:
                unit = "none"
                advance_value = 0
            plan = self.service.add_manual_item(
                platform=platform,
                group_id=group_id,
                creator_id=creator_id,
                plan_code=_text(arguments, "plan_code"),
                attraction_name=_text(arguments, "attraction_name"),
                visit_date=date.fromisoformat(_text(arguments, "visit_date")),
                advance_value=advance_value,
                advance_unit=unit,
                requires_reservation=requires,
                operation_key=operation_key,
            )
            return self.service.format_draft(plan)
        if name == "set_reservation_reminder_times":
            plan = self.service.set_draft_reminder_times(
                platform,
                group_id,
                creator_id,
                _text(arguments, "plan_code"),
                _positive_int(arguments, "item_index"),
                _times(arguments),
            )
            return self.service.format_draft(plan)
        if name == "modify_reservation_item_date":
            result = self.service.modify_item_date(
                platform,
                group_id,
                creator_id,
                _text(arguments, "item_code"),
                date.fromisoformat(_text(arguments, "visit_date")),
            )
            return self.service.format_mutation(result)
        if name == "modify_reservation_item_times":
            result = self.service.modify_item_times(
                platform,
                group_id,
                creator_id,
                _text(arguments, "item_code"),
                _times(arguments),
            )
            return self.service.format_mutation(result)
        result = self.service.cancel_item(
            platform,
            group_id,
            creator_id,
            _text(arguments, "item_code"),
        )
        return self.service.format_mutation(result)


class AgentToolRouter:
    def __init__(self, travel_service, reservation_service: ReservationService):
        self.travel_service = travel_service
        self.reservation_tools = ReservationToolExecutor(reservation_service)

    def execute(
            self,
            name: str,
            arguments: Mapping[str, object],
            context: AgentToolContext | None = None) -> str:
        if name in RESERVATION_TOOL_NAMES:
            if context is None:
                return "工具错误：预约工具缺少当前群和用户上下文。"
            try:
                return self.reservation_tools.execute(name, arguments, context)
            except (ValueError, PermissionError) as exc:
                return f"工具错误：{exc}"
        return self.travel_service.execute_tool(
            name,
            {str(key): str(value) for key, value in arguments.items()},
        )
