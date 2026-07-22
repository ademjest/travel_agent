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
            reservation_image_service: object | None = None,
            reservation_service: object | None = None,
            group_allowed: Callable[[str], bool] | None = None,
            context_builder: ContextBuilder | None = None):
        self.store = store
        self.travel_service = travel_service
        self.travel_agent = travel_agent
        self.document_service = document_service
        self.upload_binding_service = upload_binding_service
        self.outbox_worker = outbox_worker
        self.reply_renderer = reply_renderer
        self.reservation_image_service = reservation_image_service
        self.reservation_service = reservation_service
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
            image_attachments = (
                [
                    attachment
                    for attachment in event.attachments
                    if self.reservation_image_service.is_supported_attachment(
                        attachment
                    )
                ]
                if (
                    self.reservation_image_service is not None
                    and self.reservation_service is not None
                )
                else []
            )
            if len(image_attachments) > 1:
                return (
                    "一次只能识别一张预约图片，请逐张发送。",
                    memory_content or "发送多张预约图片",
                )
            if len(image_attachments) == 1:
                try:
                    result = await asyncio.to_thread(
                        self.reservation_image_service.process_attachment,
                        storage_scope_id=event.storage_scope_id,
                        platform=event.platform,
                        group_id=event.scope_id,
                        uploader_id=event.sender_id,
                        attachment=image_attachments[0],
                    )
                except ValueError as exc:
                    return (
                        f"图片处理失败：{exc}。请检查图片后重新发送。",
                        memory_content or "上传景点预约图片失败",
                    )
                except Exception:
                    logger.exception("Reservation image download failed")
                    return (
                        "图片下载失败，请稍后重新发送；"
                        "本次没有创建预约计划。",
                        memory_content or "上传景点预约图片失败",
                    )
                extraction_items = (
                    result.extraction.items
                    if result.extraction is not None
                    else ()
                )
                plan = await asyncio.to_thread(
                    self.reservation_service.create_draft,
                    result.image,
                    extraction_items,
                )
                reply = self.reservation_service.format_draft(plan)
                if result.extraction is None:
                    reply = (
                        "图片已保存，但自动识别失败，"
                        "已转为全手动草稿。\n"
                        + reply
                    )
                return reply, "上传景点预约图片"

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
                command = parse_command(event.content)
                if (
                        command.name.startswith("reservation_")
                        and self.reservation_service is not None):
                    reply = await asyncio.to_thread(
                        self.reservation_service.handle_command,
                        command,
                        event,
                    )
                elif command.name == "upload_document":
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
        except (ValueError, PermissionError) as exc:
            reply = str(exc)
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
