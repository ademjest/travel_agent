from __future__ import annotations

import asyncio
import logging
from typing import Callable

from openai import OpenAIError

from chat_transport import ChatEvent, ReplyRenderer
from commands import parse_command
from context_builder import ContextBuilder
from document_service import DocumentService
from memory_store import EventClaim, MemoryStore
from outbox_worker import OutboxWorker
from travel_agent import TravelAgent
from travel_service import TravelService
from upload_binding import UploadBindingService


logger = logging.getLogger(__name__)


class TravelBotApplication:
    def __init__(
            self,
            store: MemoryStore,
            travel_service: TravelService,
            travel_agent: TravelAgent | None,
            document_service: DocumentService,
            upload_binding_service: UploadBindingService,
            outbox_worker: OutboxWorker,
            reply_renderer: ReplyRenderer,
            group_allowed: Callable[[str], bool] | None = None,
            context_builder: ContextBuilder | None = None):
        self.store = store
        self.travel_service = travel_service
        self.travel_agent = travel_agent
        self.document_service = document_service
        self.upload_binding_service = upload_binding_service
        self.outbox_worker = outbox_worker
        self.reply_renderer = reply_renderer
        self.group_allowed = group_allowed or (lambda group_id: True)
        self.context_builder = context_builder or ContextBuilder(store)

    async def handle(self, event: ChatEvent) -> None:
        if event.channel == "group" and not self.group_allowed(event.scope_id):
            logger.warning("Ignored message from a group outside the allowlist")
            return
        if event.channel == "group":
            await asyncio.to_thread(
                self.store.save_chat_message,
                event.event_key,
                event.platform,
                event.scope_id,
                event.sender_id,
                event.event_id,
                event.reply_to_id,
                "user",
                event.content or "[附件消息]",
            )
        claim = await asyncio.to_thread(
            self.store.begin_event,
            event.event_key,
        )
        if claim is None:
            return
        try:
            reply, memory_content = await self._build_reply(event, claim)
            payload = self.reply_renderer.render(
                event.channel,
                memory_content,
                reply,
            )
            await asyncio.to_thread(
                self.store.prepare_event_outbox,
                event.event_key,
                claim.claim_token,
                event.platform,
                event.channel,
                event.scope_id,
                event.sender_id,
                event.event_id,
                payload,
                memory_content,
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.store.fail_event,
                claim.event_id,
                claim.claim_token,
                str(exc),
            )
            raise
        await self.outbox_worker.dispatch_due_once()

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
        if event.channel == "private":
            return await self._build_private_reply(event, claim)
        return await self._build_group_reply(event, memory_content)

    async def _build_group_reply(
            self,
            event: ChatEvent,
            memory_content: str) -> tuple[str, str]:
        try:
            document_result = await asyncio.to_thread(
                self.document_service.ingest_attachments,
                event.scope_id,
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
                command = parse_command(event.content)
                if command.name == "upload_document":
                    reply = await asyncio.to_thread(
                        self.upload_binding_service.issue_binding,
                        event.scope_id,
                        event.sender_id,
                    )
                elif command.name != "unknown" or not self.travel_agent:
                    reply = await asyncio.to_thread(
                        self.travel_service.handle,
                        event.content,
                    )
                else:
                    agent_context = await asyncio.to_thread(
                        self.context_builder.build,
                        event,
                    )
                    agent_result = await asyncio.to_thread(
                        self.travel_agent.run,
                        event.content,
                        agent_context,
                    )
                    reply = agent_result.reply
                    if agent_result.traces:
                        trace_text = ", ".join(
                            f"{trace.name}({trace.arguments})"
                            for trace in agent_result.traces
                        )
                        logger.info("Agent tool trace: %s", trace_text)
        except OpenAIError as exc:
            logger.error("LLM request failed: %s", exc)
            reply = (
                "LLM Agent 暂时不可用。你仍可使用“帮助”中的固定指令"
                "查询天气、路线和路况。"
            )
        except Exception:
            logger.exception("Unexpected error while handling group message")
            reply = "处理请求时出现内部错误，请稍后重试。"
        return reply, memory_content

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
