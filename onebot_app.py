from __future__ import annotations

import asyncio
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from bot_application import TravelBotApplication
from chat_transport import ChatAttachment, ChatEvent, OutgoingMessage
from document_service import DocumentService
from memory_store import MemoryStore
from outbox_worker import OutboxWorker
from reminder_scheduler import ReminderScheduler
from reservation_service import ReservationService
from settings import OneBotSettings, Settings
from travel_agent import TravelAgent
from travel_service import TravelService
from upload_binding import UploadBindingService
from vision_service import ImageVisionExtractor, ReservationImageService


class OneBotTransport:
    def __init__(
            self,
            http_url: str,
            access_token: str,
            client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=http_url.rstrip("/"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
            trust_env=False,
        )

    async def send(self, message: OutgoingMessage) -> None:
        if message.channel == "group":
            path = "/send_group_msg"
            body = {"group_id": message.target_id, **message.payload}
        else:
            path = "/send_private_msg"
            body = {"user_id": message.target_id, **message.payload}
        response = await self.client.post(path, json=body)
        response.raise_for_status()
        result = response.json()
        if (
                result.get("status") == "failed"
                or int(result.get("retcode", 0) or 0) != 0):
            raise RuntimeError(
                f"OneBot send failed: retcode={result.get('retcode')}"
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def reply_is_from_bot(
            self,
            message_id: str,
            self_id: str) -> bool:
        response = await self.client.post(
            "/get_msg",
            json={"message_id": message_id},
        )
        response.raise_for_status()
        result = response.json()
        if (
                result.get("status") == "failed"
                or int(result.get("retcode", 0) or 0) != 0):
            return False
        data = result.get("data") or {}
        sender = data.get("sender") or {}
        sender_id = str(
            sender.get("user_id")
            or data.get("user_id")
            or ""
        )
        return bool(sender_id) and sender_id == self_id


class OneBotReplyRenderer:
    def render(self, channel, command_content, reply_text):
        return {"message": reply_text}

    def render_reminder(self, recipient_id: str, text: str):
        if not recipient_id:
            return {"message": text}
        return {
            "message": [
                {"type": "at", "data": {"qq": recipient_id}},
                {"type": "text", "data": {"text": f" {text}"}},
            ],
        }


class OneBotAdapter:
    def __init__(
            self,
            settings: OneBotSettings,
            application: TravelBotApplication,
            store: MemoryStore):
        self.settings = settings
        self.application = application
        self.store = store

    async def handle(self, payload: dict[str, Any]) -> dict[str, str]:
        if payload.get("post_type") != "message":
            return {"status": "ignored"}
        message_type = str(payload.get("message_type") or "")
        if message_type == "group":
            return await self._handle_group(payload)
        if message_type == "private":
            await self.application.handle(self._private_event(payload))
            return {"status": "handled"}
        return {"status": "ignored"}

    async def _handle_group(
            self,
            payload: dict[str, Any]) -> dict[str, str]:
        group_id = str(payload.get("group_id") or "")
        if not self.settings.allows_group(group_id):
            raise HTTPException(status_code=403, detail="group not allowed")
        event, triggered = self._group_event(payload)
        if not triggered and event.reply_to_id:
            resolver = getattr(
                self.application.outbox_worker.transport,
                "reply_is_from_bot",
                None,
            )
            if resolver is not None:
                try:
                    triggered = await resolver(
                        event.reply_to_id,
                        str(payload.get("self_id") or ""),
                    )
                except Exception:
                    triggered = False
        if triggered:
            await self.application.handle(event)
            return {"status": "handled"}
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
        return {"status": "observed"}

    def _group_event(
            self,
            payload: dict[str, Any]) -> tuple[ChatEvent, bool]:
        segments = self._segments(payload)
        self_id = str(payload.get("self_id") or "")
        mentioned = any(
            segment.get("type") == "at"
            and str(segment.get("data", {}).get("qq") or "") == self_id
            for segment in segments
        )
        reply_segment = next((
            segment
            for segment in segments
            if segment.get("type") == "reply"
        ), None)
        reply_data = reply_segment.get("data", {}) if reply_segment else {}
        reply_to_id = str(reply_data.get("id") or "")
        reply_author = str(
            reply_data.get("user_id")
            or reply_data.get("qq")
            or ""
        )
        reply_to_bot = bool(payload.get("reply_to_bot")) or (
            bool(reply_segment) and reply_author == self_id
        )
        return ChatEvent(
            platform="onebot",
            channel="group",
            event_id=str(payload.get("message_id") or ""),
            scope_id=str(payload.get("group_id") or ""),
            sender_id=str(payload.get("user_id") or "unknown"),
            content=self._text_content(payload, segments),
            reply_to_id=reply_to_id,
            attachments=self._attachments(segments),
        ), mentioned or reply_to_bot

    def _private_event(self, payload: dict[str, Any]) -> ChatEvent:
        segments = self._segments(payload)
        user_id = str(payload.get("user_id") or "")
        return ChatEvent(
            platform="onebot",
            channel="private",
            event_id=str(payload.get("message_id") or ""),
            scope_id=user_id,
            sender_id=user_id,
            content=self._text_content(payload, segments),
            attachments=self._attachments(segments),
        )

    @staticmethod
    def _segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
        message = payload.get("message")
        if isinstance(message, list):
            return [item for item in message if isinstance(item, dict)]
        return []

    @staticmethod
    def _text_content(
            payload: dict[str, Any],
            segments: list[dict[str, Any]]) -> str:
        if segments:
            return "".join(
                str(segment.get("data", {}).get("text") or "")
                for segment in segments
                if segment.get("type") == "text"
            ).strip()
        raw = str(payload.get("raw_message") or payload.get("message") or "")
        raw = re.sub(r"\[CQ:(?:at|reply),[^]]+\]", "", raw)
        return raw.strip()

    @staticmethod
    def _attachments(
            segments: list[dict[str, Any]]) -> tuple[ChatAttachment, ...]:
        attachments = []
        for segment in segments:
            if segment.get("type") not in {"file", "image"}:
                continue
            data = segment.get("data", {})
            attachments.append(ChatAttachment(
                filename=str(
                    data.get("name") or data.get("file") or "attachment"
                ),
                url=str(data.get("url") or ""),
                content_type=str(data.get("content_type") or ""),
                size=int(data.get("size") or 0),
            ))
        return tuple(attachments)


def create_onebot_app(
        settings: OneBotSettings,
        application: TravelBotApplication,
        store: MemoryStore) -> FastAPI:
    adapter = OneBotAdapter(settings, application, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(
            store.delete_chat_messages_before,
            datetime.now(timezone.utc) - timedelta(days=30),
        )
        await application.reminder_scheduler.scan_once()
        await application.outbox_worker.dispatch_due_once()
        outbox_task = asyncio.create_task(
            application.outbox_worker.run(),
            name="onebot-outbox",
        )
        reminder_task = asyncio.create_task(
            application.reminder_scheduler.run(),
            name="onebot-reservation-reminders",
        )
        try:
            yield
        finally:
            outbox_task.cancel()
            reminder_task.cancel()
            await asyncio.gather(
                outbox_task,
                reminder_task,
                return_exceptions=True,
            )
            close_transport = getattr(
                application.outbox_worker.transport,
                "aclose",
                None,
            )
            if close_transport is not None:
                await close_transport()

    app = FastAPI(lifespan=lifespan)

    @app.post("/onebot")
    async def onebot_endpoint(request: Request):
        authorization = request.headers.get("Authorization", "")
        inbound_header = request.headers.get("X-OneBot-Token", "")
        provided = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else inbound_header.strip()
        )
        if not secrets.compare_digest(provided, settings.inbound_token):
            raise HTTPException(status_code=401, detail="invalid token")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid event")
        return await adapter.handle(payload)

    return app


def create_runtime_app() -> FastAPI:
    load_dotenv()
    onebot_settings = OneBotSettings.from_env()
    travel_settings = Settings(
        appid="",
        secret="",
        allowed_group_openids=onebot_settings.allowed_group_ids,
        amap_api_key=os.getenv("AMAP_API_KEY", "").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
        llm_model_id=os.getenv("LLM_MODEL_ID", "").strip(),
    )
    store = MemoryStore()
    travel_service = TravelService(travel_settings)
    travel_agent = (
        TravelAgent(travel_settings, travel_service.execute_tool)
        if travel_settings.llm_configured
        else None
    )
    document_service = DocumentService(
        store,
        summarizer=(
            travel_agent.summarize_document if travel_agent else None
        ),
    )
    upload_service = UploadBindingService(
        store,
        document_service,
        group_allowed=onebot_settings.allows_group,
    )
    image_extractor = (
        ImageVisionExtractor(
            model_id=travel_settings.llm_model_id,
            api_key=travel_settings.llm_api_key,
            base_url=travel_settings.llm_base_url,
        )
        if travel_settings.llm_configured
        else None
    )
    reservation_image_service = ReservationImageService(
        store,
        image_extractor,
    )
    reservation_service = ReservationService(store)
    transport = OneBotTransport(
        onebot_settings.http_url,
        onebot_settings.access_token,
    )
    worker = OutboxWorker("onebot", store, transport)
    reply_renderer = OneBotReplyRenderer()
    reminder_scheduler = ReminderScheduler(
        platform="onebot",
        store=store,
        renderer=reply_renderer,
        group_allowed=onebot_settings.allows_group,
    )
    application = TravelBotApplication(
        store=store,
        travel_service=travel_service,
        travel_agent=travel_agent,
        document_service=document_service,
        upload_binding_service=upload_service,
        outbox_worker=worker,
        reply_renderer=reply_renderer,
        reminder_scheduler=reminder_scheduler,
        reservation_image_service=reservation_image_service,
        reservation_service=reservation_service,
        group_allowed=onebot_settings.allows_group,
    )
    return create_onebot_app(onebot_settings, application, store)
