from __future__ import annotations

from datetime import date
from typing import Mapping

from core.event_idempotency import event_operation_key
from core.execution_scope import ensure_execution_active
from core.tool_result import ToolResult
from dataclasses import replace
from core.tool_execution import retry_read
from services.reservation_service import (
    ReservationService,
    ReservationConfirmationRequired,
    parse_beijing_datetime_list,
)
from services.reservation_draft_creator import ReservationDraftCreator
from tools.agent_tools import (
    CREATE_RESERVATION_DRAFT_TOOL,
    AgentToolContext,
    READ_TOOL_ARTIFACT_TOOL,
    RESERVATION_TOOL_NAMES,
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
    def __init__(
            self,
            service: ReservationService,
            draft_creator: ReservationDraftCreator):
        self.service = service
        self.draft_creator = draft_creator

    def execute(
            self,
            name: str,
            arguments: Mapping[str, object],
            context: AgentToolContext) -> str:
        ensure_execution_active()
        if name not in RESERVATION_TOOL_NAMES:
            raise ValueError(f"未知预约工具 {name}")

        normalized_arguments = dict(arguments)
        operation_key = event_operation_key(
            context.event_id,
            name,
            normalized_arguments,
        )
        cacheable = name not in {"list_reservation_plans", "refresh_reservation_plan"}
        cached = self.service.store.get_event_tool_result(operation_key) if cacheable else None
        if cached is not None:
            return cached

        result = self._execute_uncached(
            name,
            normalized_arguments,
            context,
            operation_key,
        )
        if not cacheable:
            return result
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

        if name == CREATE_RESERVATION_DRAFT_TOOL:
            return self.draft_creator.create(
                platform=platform,
                group_id=group_id,
                creator_id=creator_id,
                event_id=context.event_id,
                attachments=context.attachments,
                attachment_index=_positive_int(
                    arguments,
                    "attachment_index",
                ),
            )
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
        if name == "update_reservation_draft_item":
            visit_date_text = str(arguments.get("visit_date") or "").strip()
            requires = arguments.get("requires_reservation")
            if requires is not None and not isinstance(requires, bool):
                raise ValueError("requires_reservation 必须是布尔值")
            visit_status = str(arguments.get("visit_status") or "").strip()
            if visit_status and visit_status not in {"visit", "skip"}:
                raise ValueError("visit_status 必须是 visit 或 skip")
            if not visit_date_text and requires is None and not visit_status:
                raise ValueError("至少需要提供一项草稿修改")
            plan = self.service.update_draft_item(
                platform=platform,
                group_id=group_id,
                creator_id=creator_id,
                plan_code=_text(arguments, "plan_code"),
                item_index=_positive_int(arguments, "item_index"),
                visit_date=(
                    date.fromisoformat(visit_date_text)
                    if visit_date_text
                    else None
                ),
                requires_reservation=requires,
                visit_status=visit_status or None,
            )
            return self.service.format_draft(plan)
        if name == "update_reservation_draft_items":
            raw_updates = arguments.get("updates")
            if (
                    not isinstance(raw_updates, list)
                    or not raw_updates
                    or len(raw_updates) > 20):
                raise ValueError("updates 必须包含 1 到 20 项修改")
            updates = []
            for raw_update in raw_updates:
                if not isinstance(raw_update, Mapping):
                    raise ValueError("updates 中的每一项必须是对象")
                attraction_name = _text(raw_update, "attraction_name")
                visit_date_text = str(
                    raw_update.get("visit_date") or ""
                ).strip()
                requires = raw_update.get("requires_reservation")
                if requires is not None and not isinstance(requires, bool):
                    raise ValueError("requires_reservation 必须是布尔值")
                visit_status = str(
                    raw_update.get("visit_status") or ""
                ).strip()
                if visit_status and visit_status not in {"visit", "skip"}:
                    raise ValueError("visit_status 必须是 visit 或 skip")
                if not visit_date_text and requires is None and not visit_status:
                    raise ValueError(f"景点 {attraction_name} 没有提供修改内容")
                updates.append({
                    "attraction_name": attraction_name,
                    "visit_date": (
                        date.fromisoformat(visit_date_text)
                        if visit_date_text
                        else None
                    ),
                    "requires_reservation": requires,
                    "visit_status": visit_status or None,
                })
            plan = self.service.update_draft_items(
                platform=platform,
                group_id=group_id,
                creator_id=creator_id,
                plan_code=_text(arguments, "plan_code"),
                updates=updates,
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
    def __init__(
            self,
            travel_service,
            reservation_service: ReservationService,
            draft_creator: ReservationDraftCreator,
            artifact_repository=None):
        self.travel_service = travel_service
        self.reservation_tools = ReservationToolExecutor(
            reservation_service,
            draft_creator,
        )
        self.artifact_repository = artifact_repository

    def execute_result(
            self, name, arguments, context=None) -> ToolResult:
        try:
            result = self._execute_result(name, arguments, context)
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

    def _execute_result(
            self,
            name: str,
            arguments: Mapping[str, object],
            context: AgentToolContext | None = None) -> ToolResult:
        resource_id = str(arguments.get("plan_code") or arguments.get("item_code") or "")
        if name == READ_TOOL_ARTIFACT_TOOL:
            if context is None or self.artifact_repository is None:
                return ToolResult('failed', name, data='工具结果存储暂不可用。', error_code='not_available')
            try:
                page = self.artifact_repository.read(
                    context, str(arguments.get('artifact_id') or ''),
                    int(arguments.get('cursor') or 0), int(arguments.get('max_chars') or 4000))
                return ToolResult('completed', name, page['artifact_id'], data=__import__('json').dumps(page, ensure_ascii=False))
            except PermissionError as exc:
                return ToolResult('failed', name, data=str(exc), error_code='permission_denied')
            except (TypeError, ValueError) as exc:
                return ToolResult('failed', name, data=str(exc), error_code='invalid_argument')
        if name in RESERVATION_TOOL_NAMES and context is not None:
            try:
                text = self.reservation_tools.execute(name, arguments, context)
            except ReservationConfirmationRequired as exc:
                return ToolResult("needs_input", name, resource_id, data=str(exc), error_code="review_required")
            except (ValueError, PermissionError) as exc:
                return ToolResult("failed", name, resource_id, data=str(exc),
                                  error_code="permission_denied" if isinstance(exc, PermissionError) else "invalid_state_or_arguments")
        else:
            if callable(getattr(type(self.travel_service),'execute_result',None)):
                typed = self._read_with_receipt(name,arguments,context)
            else:
                typed = ToolResult.from_text(name, self.execute(name,arguments,context))
            text = typed.data
        if (context is not None and self.artifact_repository is not None
                and name not in RESERVATION_TOOL_NAMES and len(text) > 6000):
            result = typed if name not in RESERVATION_TOOL_NAMES else ToolResult.from_text(name,text,resource_id)
            artifact_id = self.artifact_repository.save(context, name, text)
            text = text[:4000].rstrip() + (
                f'\n[结果已保存为 {artifact_id}；存储上限500000字符，超出部分未保存。需要更多内容时调用 {READ_TOOL_ARTIFACT_TOOL} 分页读取。]')
            return replace(result,data=text,artifact_id=artifact_id)
        if name not in RESERVATION_TOOL_NAMES:
            return typed
        result = ToolResult.from_text(name, text, resource_id)
        return replace(result,effect_status='committed' if result.status=='completed' and name!='list_reservation_plans' else 'none',
                       operation_key=event_operation_key(context.event_id,name,arguments) if context else '')

    def _read_with_receipt(self,name,arguments,context):
        import json
        store = self.artifact_repository.store if self.artifact_repository else None
        max_attempts = 1 if getattr(self.travel_service, 'manages_read_retries', False) is True else 3
        if store is None or context is None or store.get_event_status(context.event_id) is None:
            return retry_read(lambda: self.travel_service.execute_result(name,arguments), max_attempts=max_attempts)
        from infrastructure.place_resolution import PLACE_RESOLUTION_VERSION
        version=':'+PLACE_RESOLUTION_VERSION if name in {'get_driving_route','get_route_traffic','get_walking_route','get_transit_route'} else ''
        key = event_operation_key(context.event_id,'read:'+name+version,arguments)
        cached = store.get_event_tool_result(key)
        if cached is not None:
            return ToolResult(**json.loads(cached))
        def operation():
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                attempts = db.execute('SELECT attempts FROM event_read_attempts WHERE operation_key=?',(key,)).fetchone()
                total = db.execute('SELECT coalesce(sum(attempts),0) FROM event_read_attempts WHERE event_id=?',(context.event_id,)).fetchone()[0]
                if (attempts and attempts['attempts']>=3) or total>=12:
                    return ToolResult('failed',name,data='本次查询重试额度已用完，请稍后重新发起。',error_code='attempt_limit')
                db.execute('''INSERT INTO event_read_attempts VALUES (?,?,1)
                    ON CONFLICT(operation_key) DO UPDATE SET attempts=attempts+1''',(key,context.event_id))
            return self.travel_service.execute_result(name,arguments)
        result=retry_read(operation, max_attempts=max_attempts)
        if result.status=='completed':
            store.save_event_tool_result(key,context.event_id,name,arguments,result.to_json())
        return result

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
