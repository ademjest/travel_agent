from __future__ import annotations

import asyncio
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from background_supervisor import BackgroundSupervisor
from bot_application import TravelBotApplication
from chat_transport import ChatAttachment, ChatEvent, OutgoingMessage
from maintenance import MaintenanceService
from memory_store import MemoryStore
from runtime_factory import build_runtime
from settings import OneBotSettings, Settings


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
            self._required_id(payload, "message_id")
            self._required_id(payload, "group_id")
            self._required_id(payload, "user_id")
            self._required_id(payload, "self_id")
            return await self._handle_group(payload)
        if message_type == "private":
            self._required_id(payload, "message_id")
            self._required_id(payload, "user_id")
            await self.application.handle(self._private_event(payload))
            return {"status": "handled"}
        return {"status": "ignored"}

    @staticmethod
    def _required_id(payload: dict[str, Any], name: str) -> str:
        value = str(payload.get(name) or "").strip()
        if not value:
            raise HTTPException(
                status_code=400,
                detail=f"missing required field: {name}",
            )
        if len(value) > 128:
            raise HTTPException(
                status_code=400,
                detail=f"invalid field: {name}",
            )
        return value

    async def _handle_group(
            self,
            payload: dict[str, Any]) -> dict[str, str]:
        group_id = self._required_id(payload, "group_id")
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
        self_id = self._required_id(payload, "self_id")
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
            event_id=self._required_id(payload, "message_id"),
            scope_id=self._required_id(payload, "group_id"),
            sender_id=self._required_id(payload, "user_id"),
            content=self._text_content(payload, segments),
            reply_to_id=reply_to_id,
            attachments=self._attachments(segments),
        ), mentioned or reply_to_bot

    def _private_event(self, payload: dict[str, Any]) -> ChatEvent:
        segments = self._segments(payload)
        user_id = self._required_id(payload, "user_id")
        return ChatEvent(
            platform="onebot",
            channel="private",
            event_id=self._required_id(payload, "message_id"),
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
        store: MemoryStore,
        maintenance_service: MaintenanceService | None = None,
        supervisor: BackgroundSupervisor | None = None) -> FastAPI:
    adapter = OneBotAdapter(settings, application, store)
    maintenance = maintenance_service or MaintenanceService(
        store,
        Path(__file__).resolve().parent / "data" / "images",
    )
    task_supervisor = supervisor or BackgroundSupervisor()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(maintenance.run_once)
        await application.reminder_scheduler.scan_once()
        await application.outbox_worker.dispatch_due_once()
        task_supervisor.start(
            "onebot-outbox",
            application.outbox_worker.run,
        )
        task_supervisor.start(
            "onebot-reservation-reminders",
            application.reminder_scheduler.run,
        )
        task_supervisor.start("onebot-maintenance", maintenance.run)
        app.state.background_supervisor = task_supervisor
        try:
            yield
        finally:
            await task_supervisor.stop()
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

    @app.get("/health")
    async def health_endpoint():
        storage = await asyncio.to_thread(
            store.runtime_health,
            "onebot",
        )
        tasks = task_supervisor.snapshot()
        for name in (
                "onebot-outbox",
                "onebot-reservation-reminders",
                "onebot-maintenance"):
            tasks.setdefault(name, {
                "running": False,
                "restart_count": 0,
                "last_error": "",
                "last_failure_at": "",
            })
        degraded = (
            int(storage["dead_letters"]) > 0
            or int(storage["stale_processing_events"]) > 0
            or any(not bool(item["running"]) for item in tasks.values())
            or bool(maintenance.last_error)
        )
        return {
            "status": "degraded" if degraded else "ok",
            "tasks": tasks,
            "storage": storage,
            "maintenance": {
                "last_run_at": (
                    maintenance.last_run_at.isoformat()
                    if maintenance.last_run_at
                    else ""
                ),
                "last_error": maintenance.last_error,
            },
        }

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
    transport = OneBotTransport(
        onebot_settings.http_url,
        onebot_settings.access_token,
    )
    reply_renderer = OneBotReplyRenderer()
    runtime = build_runtime(
        travel_settings,
        platform="onebot",
        transport=transport,
        reply_renderer=reply_renderer,
        group_allowed=onebot_settings.allows_group,
    )
    return create_onebot_app(
        onebot_settings,
        runtime.application,
        runtime.store,
        runtime.maintenance_service,
        runtime.supervisor,
    )
