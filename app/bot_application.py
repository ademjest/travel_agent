from __future__ import annotations
from infrastructure.skill_manager import skill_request
from tools.trip.tool import PlanningArtifacts

import asyncio
from contextlib import suppress
import logging
import json
import re
from typing import Callable

from openai import OpenAIError

from app.conversation_context import ContextBuilder
from agents.travel_agent import TravelAgent, run_read_agent
from core.chat_transport import ChatEvent, OutgoingMessage, ReplyRenderer
from core.commands import ONEBOT_UPLOAD_DOCUMENT_TEXT, parse_command
from core.execution_scope import CURRENT_EXECUTION, ExecutionRevoked
from core.model_budget import MODEL_EVENT, MODEL_RUN
from core.logging_config import exception_metadata
from app.request_router import bind_request, CURRENT_REQUEST, RequestRouter, trip_command
from core.tasks import READ_TASK_TYPES
from core.execution_outcome import TravelReadRequest, CONVERSATION_POLICY
import hashlib
from infrastructure.delivery_repository import EventClaim
from infrastructure.memory_store import MemoryStore
from tools.materials import MaterialsTool
from services.outbox_worker import OutboxWorker
from tools.travel_query import TravelQueryTool
from adapters.upload_binding import UploadBindingService
from infrastructure.image_inputs import is_supported_attachment
from tools.trip.tool import trip_request
from core.tool_result import AgentToolContext
from core.tool_result import CREATE_RESERVATION_DRAFT_TOOL


logger = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 8_000
MAX_REPLY_CHARS = 8_000
MAX_ATTACHMENTS = 8


class TravelBotApplication:
    def __init__(
            self,
            store: MemoryStore,
            travel_service: TravelQueryTool,
            travel_agent: TravelAgent | None,
            document_service: MaterialsTool,
            upload_binding_service: UploadBindingService,
            outbox_worker: OutboxWorker,
            reply_renderer: ReplyRenderer,
            reminder_scheduler: object,
            reservation_service: object | None = None,
            tool_router: object | None = None,
            group_allowed: Callable[[str], bool] | None = None,
            context_builder: ContextBuilder | None = None,
            event_lease_renew_seconds: float = 60.0,
            personal_reminder_service=None,
            booking_reminder_service=None,
            trip_service=None,
            image_context_service=None,
            scheduled_query_service=None,
            policy_watch_service=None,
            semantic_task_service=None, read_only_service=None, reminders_tool=None, research_service=None):
        self.store = store
        self.travel_service = travel_service
        self.travel_agent = travel_agent
        self.document_service = document_service
        self.upload_binding_service = upload_binding_service
        self.outbox_worker = outbox_worker
        self.reply_renderer = reply_renderer
        self.reminder_scheduler = reminder_scheduler
        self.reservation_service = reservation_service
        self.tool_router = tool_router
        self.group_allowed = group_allowed or (lambda group_id: True)
        self.context_builder = context_builder or ContextBuilder(store)
        self.event_lease_renew_seconds = event_lease_renew_seconds
        self.personal_reminder_service = personal_reminder_service
        self.booking_reminder_service = booking_reminder_service
        self.trip_service = trip_service
        self.research_service = research_service
        self.image_context_service = image_context_service
        self.scheduled_query_service = scheduled_query_service
        self.policy_watch_service = policy_watch_service
        self.reminders_tool = (reminders_tool or personal_reminder_service or booking_reminder_service
                               or scheduled_query_service or policy_watch_service)
        self.semantic_task_service = semantic_task_service
        from tools.travel_query import TravelQueryTool
        self.read_only_service = read_only_service or TravelQueryTool(store=store, backend=travel_service, tool_executor=tool_router.execute_result if tool_router else None)
        self._reservation_processing: set[tuple[str, str, str]] = set()
        self._document_processing: set[tuple[str, str, str]] = set()

    @skill_request
    async def handle(self, event: ChatEvent) -> None:
        binding = bind_request(self.store,event)
        token = MODEL_EVENT.set(event.event_key)
        binding_token = CURRENT_REQUEST.set(binding)
        run_token = MODEL_RUN.set(binding.budget_run_key)
        try:
            await self._handle(event)
        finally:
            MODEL_EVENT.reset(token)
            MODEL_RUN.reset(run_token)
            CURRENT_REQUEST.reset(binding_token)

    async def _handle(self, event: ChatEvent) -> None:
        if event.channel == "group" and not self.group_allowed(event.scope_id):
            logger.warning("Ignored message from a group outside the allowlist")
            return
        if event.channel == "group":
            await asyncio.to_thread(
                self.store.conversations.save_chat_message,
                event.event_key,
                event.platform,
                event.scope_id,
                event.sender_id,
                event.event_id,
                event.reply_to_id,
                "user",
                (event.content or "[附件消息]")[:MAX_MESSAGE_CHARS],
            )
        await asyncio.to_thread(self.store.diagnostics.record,'request',{'event_key':event.event_key,'status':'received'})
        claim = await asyncio.to_thread(
            self.store.delivery.begin_event,
            event.event_key,
        )
        await asyncio.to_thread(self.store.diagnostics.record, 'request',
            {'event_key': event.event_key, 'status': 'claimed' if claim else 'already_claimed'})
        if claim is None:
            return
        lease_task = asyncio.create_task(
            self._renew_event_lease(claim),
            name=f"event-lease:{event.event_key}",
        )
        try:
            reply, memory_content = await self._build_reply(event, claim)
            if len(reply) > MAX_REPLY_CHARS:
                reply = reply[:MAX_REPLY_CHARS - 12] + "\n[回复已截断]"
            memory_content = memory_content[:MAX_MESSAGE_CHARS]
            payload = self.reply_renderer.render(
                event.channel,
                memory_content,
                reply,
            )
            await asyncio.to_thread(
                self.store.delivery.prepare_event_outbox,
                event.event_key,
                claim.claim_token,
                event.platform,
                event.channel,
                event.scope_id,
                event.sender_id,
                event.event_id,
                payload,
                memory_content,
                assistant_text=reply,
            )
            await asyncio.to_thread(self.store.diagnostics.record, 'request',
                {'event_key': event.event_key, 'status': 'output_prepared'})
        except ExecutionRevoked:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.store.delivery.fail_event,
                claim.event_id,
                claim.claim_token,
                str(exc),
            )
            raise
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError, ExecutionRevoked):
                await lease_task
        if CURRENT_EXECUTION.get() is None:
            await self.outbox_worker.dispatch_due_once()

    async def _renew_event_lease(self, claim: EventClaim) -> None:
        while True:
            await asyncio.sleep(self.event_lease_renew_seconds)
            renewed = self.store.delivery.renew_event(
                claim.event_id,
                claim.claim_token,
            )
            if not renewed:
                logger.warning("Lost event processing lease: %s", claim.event_id)
                return

    async def _build_reply(
            self,
            event: ChatEvent,
            claim: EventClaim) -> tuple[str, str]:
        memory_content = event.content.strip()
        if claim.prepared_reply is not None:
            return (
                claim.prepared_reply,
                claim.prepared_memory_content or memory_content,
            )
        if len(event.content) > MAX_MESSAGE_CHARS:
            return (
                "消息正文超过 8000 字符限制，请精简后重试。",
                "消息过长",
            )
        if len(event.attachments) > MAX_ATTACHMENTS:
            return (
                "一次最多处理 8 个附件，请分批发送。",
                "附件数量过多",
            )
        if event.channel == "private":
            return await self._build_private_reply(event, claim)
        return await self._build_conversation_reply(event, memory_content, claim)

    async def _build_group_reply(
            self, event: ChatEvent, memory_content: str, claim: EventClaim) -> tuple[str, str]:
        return await self._build_conversation_reply(event, memory_content, claim)

    async def _finish_research_use_case(self, event, claim, answer):
        if (self.trip_service is None or not trip_request(event.content)
                or not re.search(r'规划|制定|安排', event.content)):
            return answer
        material = await asyncio.to_thread(self.research_service.read_for_planning, event)
        if material.source is None:
            return answer if not material.note or material.note in answer else answer + '\n' + material.note
        trip_reply = await asyncio.to_thread(self.trip_service.handle, event, claim,
                                              source_text_override=material.source)
        if not trip_reply:
            return answer
        return trip_reply if event.content.startswith('根据研究结果') or material.provider_answer else answer + '\n\n' + trip_reply

    async def _build_conversation_reply(
            self,
            event: ChatEvent,
            memory_content: str,
            claim: EventClaim) -> tuple[str, str]:
        try:
            read_request = None
            context_decision = CONVERSATION_POLICY
            if not event.attachments or event.trip_edit is not None:
                router = self.semantic_task_service or RequestRouter(self.store,None,
                    trip_service=self.trip_service,reminder_service=self.reminders_tool,research_service=self.research_service,
                    read_only_service=self.read_only_service,preferences_tool=getattr(self,'preference_service',None),
                    planning_tool=getattr(self,'planning_service',None),reservation_tool=self.reservation_service)
                routed,context_decision = await asyncio.to_thread(router.route,event,claim)
                if isinstance(routed,TravelReadRequest):
                    read_request = routed
                elif routed is not None:
                    return routed,memory_content
            command = parse_command(event.content)
            workflow_key = (
                event.platform,
                event.scope_id,
                event.sender_id,
            )
            if (
                    command.name == "reservation_stop"
                    and self.reservation_service is not None):
                reply = await asyncio.to_thread(
                    self.reservation_service.handle_command,
                    command,
                    event,
                )
                return reply, memory_content

            image_attachments = [
                attachment
                for attachment in event.attachments
                if is_supported_attachment(attachment)
            ]
            reservation_workflow_active = False
            if self.reservation_service is not None:
                if (
                        image_attachments
                        and command.name == "reservation_start"):
                    await asyncio.to_thread(
                        self.reservation_service.start_workflow,
                        event.platform,
                        event.scope_id,
                        event.sender_id,
                    )
                reservation_workflow_active = (
                    command.name == "reservation_start"
                    or await asyncio.to_thread(
                        self.reservation_service.workflow_is_active,
                        event.platform,
                        event.scope_id,
                        event.sender_id,
                    )
                )

            if image_attachments and not reservation_workflow_active:
                if self.image_context_service is not None:
                    result = await asyncio.to_thread(self.image_context_service.analyze, event, image_attachments)
                    if self.trip_service is not None and trip_command(event.content):
                        if result['uncertain']:
                            return '图片关键信息尚不明确，还不能据此建立行程或提醒。\n' + result['answer'], memory_content
                        source = f"[图片#{result['media_id']}]\n{result['facts']}"
                        reply = await asyncio.to_thread(self.trip_service.handle, event, claim, source_text_override=source)
                        if reply is not None:
                            return reply, memory_content
                    prefix = '图片部分字段不清晰，请核对原图。\n' if result['uncertain'] else '图片中显示的信息：\n'
                    return prefix + result['answer'], memory_content
                return (
                    "图片不会自动创建预约计划。"
                    "如果这是预约攻略，请先发送“制定预约”，"
                    "或在图片消息中填写“制定预约”。",
                    memory_content or "发送普通图片",
                )
            if len(image_attachments) > 1:
                return (
                    "一次只能识别一张预约图片，请逐张发送。",
                    memory_content or "发送多张预约图片",
                )
            document_attachments = [
                attachment
                for attachment in event.attachments
                if MaterialsTool.is_document_attachment(attachment)
            ]
            if image_attachments and document_attachments:
                return (
                    "请不要在同一条消息中混合发送预约图片和旅行文档；"
                    "请拆成两条消息分别发送。",
                    memory_content or "混合发送预约图片和旅行文档",
                )
            if (
                    not document_attachments
                    and workflow_key in self._document_processing
                    and self._asks_about_document_processing(event.content)):
                return (
                    "已经收到你上传的文档，目前仍在解析并写入群共享资料。"
                    "完成后会自动发送保存结果，请稍候，不需要重复上传。",
                    memory_content,
                )
            if len(image_attachments) == 1 and reservation_workflow_active:
                if workflow_key in self._reservation_processing:
                    return (
                        "上一张预约攻略图片仍在识别，请等待当前草稿完成，"
                        "不要重复发送图片。",
                        memory_content or "重复发送预约图片",
                    )
                if self.tool_router is None:
                    return (
                        "预约图片工具暂不可用，请稍后重试。",
                        memory_content or "上传景点预约图片失败",
                    )
                self._reservation_processing.add(workflow_key)
                try:
                    await self._send_onebot_progress(
                        event,
                        "已收到预约攻略图片，正在下载并识别。"
                        "处理通常需要几十秒，完成后会自动发送预约草稿，"
                        "请勿重复发送。",
                    )
                    reply = await asyncio.to_thread(
                        self.tool_router.execute,
                        CREATE_RESERVATION_DRAFT_TOOL,
                        {"attachment_index": 1},
                        self._tool_context(event),
                    )
                finally:
                    self._reservation_processing.discard(workflow_key)
                reply = reply.removeprefix("工具错误：")
                return reply, "上传景点预约图片"

            if document_attachments:
                if workflow_key in self._document_processing:
                    return (
                        "上一份文档仍在解析，请等待当前处理完成后再上传。",
                        memory_content or "重复上传旅行文档",
                    )
                self._document_processing.add(workflow_key)
                try:
                    filenames = "、".join(
                        attachment.filename
                        for attachment in document_attachments
                    )
                    await self._send_onebot_progress(
                        event,
                        f"已收到文档：{filenames}。正在下载并解析，"
                        "完成后会自动发送保存结果，请勿重复上传。",
                    )
                    document_result = await asyncio.to_thread(
                        self.document_service.ingest_attachments,
                        event.storage_scope_id,
                        event.sender_id,
                        list(event.attachments),
                    )
                finally:
                    self._document_processing.discard(workflow_key)
            else:
                document_result = await asyncio.to_thread(
                    self.document_service.ingest_attachments,
                    event.storage_scope_id,
                    event.sender_id,
                    list(event.attachments),
                )
            if document_result.handled:
                reply = document_result.reply
                memory_content = (
                    document_result.memory_content
                    or memory_content
                    or "上传旅行文档"
                )
            else:
                if (
                        command.name.startswith("reservation_")
                        and self.reservation_service is not None):
                    reply = await asyncio.to_thread(
                        self.reservation_service.handle_command,
                        command,
                        event,
                    )
                elif command.name == "upload_document":
                    if event.platform == "web":
                        reply = '点击输入框旁的附件按钮，上传 TXT、Markdown、Word 或 Excel 旅行资料；上传后发送即可导入当前会话。'
                    elif event.platform == "onebot":
                        reply = ONEBOT_UPLOAD_DOCUMENT_TEXT
                    else:
                        reply = await asyncio.to_thread(
                            self.upload_binding_service.issue_binding,
                            event.scope_id,
                            event.sender_id,
                            event_id=event.event_key,
                            claim_token=claim.claim_token,
                        )
                elif (
                        reservation_workflow_active
                        and command.name == "unknown"):
                    if workflow_key in self._reservation_processing:
                        reply = (
                            "已收到上一张预约攻略图片，目前仍在识别。"
                            "完成后会自动发送预约草稿，请稍候。"
                        )
                    else:
                        reply = (
                            "当前正在制定预约。请发送一张预约攻略图片，"
                            "或发送“退出制定预约”结束当前流程。"
                        )
                elif command.name != "unknown" or not self.travel_agent:
                    reply = await asyncio.to_thread(
                        self.travel_service.handle,
                        event.content,
                    )
                else:
                    document_reference = context_decision is not None and ('document' in context_decision.intents
                        or bool(re.search(r'我们.{0,8}住|住哪里|住哪|住宿安排|之前.{0,8}(?:行程|安排)', event.content)))
                    if read_request is not None:
                        context_purpose = 'travel_lookup'
                    elif document_reference and set(context_decision.intents) <= {'places', 'document', 'general'}:
                        context_purpose = 'document_qa'
                    elif set(context_decision.intents) & READ_TASK_TYPES:
                        context_purpose = 'travel_lookup'
                    else:
                        context_purpose = 'tool_reasoning'
                    agent_context = await asyncio.to_thread(
                        self.context_builder.build,
                        event,
                        context_purpose,
                    )
                    if read_request is not None or set(context_decision.intents) <= READ_TASK_TYPES:
                        agent_result = await asyncio.to_thread(run_read_agent,event,read_request,
                            self.travel_agent,agent_context,self._tool_context(event),decision=context_decision,record=self.read_only_service.record)
                    elif isinstance(self.travel_agent, TravelAgent):
                        agent_result = await asyncio.to_thread(
                            self.travel_agent.run,
                            event.content,
                            agent_context,
                            "",
                            self._tool_context(event),
                            read_request=read_request,
                            decision_override=context_decision,
                        )
                    else:
                        agent_result = await asyncio.to_thread(
                            self.travel_agent.run,
                            event.content,
                            agent_context,
                        )
                    reply = agent_result.reply
                    if getattr(self.store,'planning',None) is not None:
                        try:
                            await asyncio.to_thread(PlanningArtifacts(self.store.planning).capture,event,reply,kind='exploratory')
                        except (ValueError, OSError):
                            logger.info('Planning artifact capture skipped: event=%s',event.event_key)
                    if isinstance(self.travel_agent, TravelAgent) and agent_result.task_update:
                        await asyncio.to_thread(
                            self.store.tasks.prepare_result, event, claim,
                            agent_result.task_update, reply,
                        )
                    if agent_result.traces:
                        logger.info('Agent tool trace: %s', json.dumps({'event_id': event.event_key,
                            'calls': [{'name': trace.name, 'call_id': getattr(trace, 'call_id', '')}
                                      for trace in agent_result.traces]}, ensure_ascii=False))
        except ExecutionRevoked:
            raise
        except (ValueError, PermissionError) as exc:
            if getattr(exc,'details',{}).get('code')=='input_projection_limit':
                self.store.diagnostics.record('planning',{'event_key':event.event_key,'status':'failed',
                    'name':exc.details['path'],'error_code':'input_projection_limit',
                    'validation':exc.details,'input_parts':exc.input_parts})
            reply = str(exc)
        except OpenAIError as exc:
            logger.error("LLM request failed: %s", exc)
            reply = (
                "LLM Agent 暂时不可用。你仍可使用“帮助”中的固定指令"
                "查询天气、路线和路况。"
            )
        except Exception as error:
            self.store.diagnostics.record('request',{'event_key':event.event_key,'status':'business_failed',**exception_metadata(error)})
            logger.exception("Unexpected error while handling group message")
            reply = ("处理请求时出现内部错误，可打开“查看处理过程”查看原因。" if event.platform=='web'
                     else "处理请求时出现内部错误，请稍后重试。")
        return reply, memory_content

    @staticmethod
    def _asks_about_document_processing(content: str) -> bool:
        normalized = content.lower()
        return any(term in normalized for term in (
            "文档",
            "文件",
            "表格",
            "上传",
            "xlsx",
            "excel",
            "docx",
        ))

    async def _send_onebot_progress(
            self,
            event: ChatEvent,
            text: str) -> None:
        if event.platform not in {"onebot", "web"}:
            return
        payload = self.reply_renderer.render(
            event.channel,
            event.content,
            text,
        )
        if event.platform == 'web':
            payload['kind'] = 'progress'
        try:
            await asyncio.wait_for(
                self.outbox_worker.transport.send(OutgoingMessage(
                    channel=event.channel,
                    target_id=event.scope_id,
                    reply_to_id=event.event_id,
                    payload=payload,
                    delivery_key=f'progress:{event.event_key}:{text}' if event.platform == 'web' else '',
                )),
                timeout=5,
            )
        except Exception:
            logger.warning(
                "Failed to send OneBot progress: event_id=%s",
                event.event_key,
            )

    def _tool_context(self,event: ChatEvent) -> AgentToolContext:
        from app.request_router import bind_request
        from app.conversation_context import lodging_context
        bound=bind_request(self.store,event).read_context;lodging=lodging_context(self.store,event)
        known={k:lodging[k] for k in ('targets','stay') if lodging.get(k)}
        if not known.get('targets') and lodging.get('discovery',{}).get('city'):known['discovery']=lodging['discovery']
        return AgentToolContext(
            platform=event.platform,
            group_id=event.scope_id,
            creator_id=event.sender_id,
            event_id=event.event_key,
            attachments=event.attachments,
            event=event,
            execution_data={**bound.slots,'reference_time':bound.reference_time,'hotel_request':bound.slots.get('hotel_request',known),
                'hotel_task':bound.task if bound.task and bound.task.task_type=='hotel' else None,
                'hotel_collecting':bool(bound.task and bound.task.status=='collecting'),
                'hotel_date_proposal':bound.slots.get('hotel_date_proposal',lodging.get('proposed_stay',{})),
                'source_context':bound.slots.get('source_context',lodging.get('source_context',{}))},
        )

    async def _build_private_reply(
            self,
            event: ChatEvent,
            claim: EventClaim) -> tuple[str, str]:
        try:
            result = await asyncio.to_thread(
                self.upload_binding_service.handle_private_message,
                event.sender_id,
                event.content.strip(),
                list(event.attachments),
                event_id=event.event_key,
                claim_token=claim.claim_token,
                platform=event.platform,
                reply_to_id=event.event_id,
            )
            return result.reply, ""
        except Exception:
            logger.exception("Unexpected error while handling private message")
            return "处理私聊文件时出现内部错误，请稍后重试。", ""
