from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from app.bot_application import TravelBotApplication
from app.group_trigger_policy import GroupTriggerPolicy
from app.runtime_factory import build_runtime
from core.background_supervisor import BackgroundSupervisor
from core.chat_transport import ChatAttachment, ChatEvent, OutgoingMessage
from core.commands import ONEBOT_HELP_TEXT, parse_command
from core.settings import OneBotSettings, Settings
from infrastructure.memory_store import MemoryStore
from services.maintenance import MaintenanceService


logger = logging.getLogger(__name__)


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

    async def get_group_file_url(
            self,
            group_id: str,
            file_id: str) -> str:
        response = await self.client.post(
            "/get_group_file_url",
            json={
                "group_id": group_id,
                "file_id": file_id,
            },
        )
        response.raise_for_status()
        result = response.json()
        if (
                result.get("status") == "failed"
                or int(result.get("retcode", 0) or 0) != 0):
            raise RuntimeError(
                "OneBot group file URL failed: "
                f"retcode={result.get('retcode')}"
            )
        url = str((result.get("data") or {}).get("url") or "").strip()
        if not url:
            raise RuntimeError("OneBot group file URL response has no data.url")
        return url

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
        if parse_command(command_content).name == "help":
            reply_text = ONEBOT_HELP_TEXT
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
        self.transport = application.outbox_worker.transport
        self.trigger_policy = GroupTriggerPolicy(store)

    async def handle(self, payload: dict[str, Any]) -> dict[str, str]:
        post_type = str(payload.get("post_type") or "")
        if post_type == "message":
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
        if (
                post_type == "notice"
                and payload.get("notice_type") == "group_upload"):
            return await self._handle_group_upload(payload)
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
        if (
                self._required_id(payload, "user_id")
                == self._required_id(payload, "self_id")):
            return {"status": "ignored"}

        segments = self._segments(payload)
        event, triggered = self._group_event(payload, segments)
        if not triggered:
            triggered = await self.trigger_policy.should_handle(event)
        if not triggered and event.reply_to_id:
            resolver = getattr(
                self.transport,
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
            event_status = await asyncio.to_thread(
                self.store.get_event_status,
                event.event_key,
            )
            if event_status == "completed":
                return {"status": "handled"}
            try:
                segments = await self._resolve_group_file_urls(
                    group_id,
                    segments,
                )
            except HTTPException as exc:
                has_group_file = any(
                    segment.get("type") == "file"
                    for segment in segments
                )
                if has_group_file and exc.status_code == 502:
                    logger.info(
                        "Deferred group file message until upload notice: "
                        "group_id=%s event_id=%s",
                        group_id,
                        event.event_id,
                    )
                    return {"status": "deferred"}
                raise
            event, _ = self._group_event(payload, segments)
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

    async def _handle_group_upload(
            self,
            payload: dict[str, Any]) -> dict[str, str]:
        group_id = self._required_id(payload, "group_id")
        if not self.settings.allows_group(group_id):
            raise HTTPException(status_code=403, detail="group not allowed")
        user_id = self._required_id(payload, "user_id")
        self_id = self._required_id(payload, "self_id")
        if user_id == self_id:
            return {"status": "ignored"}

        file_data = payload.get("file")
        if not isinstance(file_data, dict):
            raise HTTPException(
                status_code=400,
                detail="missing group upload file",
            )
        file_id = str(
            file_data.get("file_id")
            or file_data.get("id")
            or file_data.get("file")
            or ""
        ).strip()
        if not file_id:
            raise HTTPException(status_code=400, detail="missing group file id")

        segments = [{
            "type": "file",
            "data": {
                "file_id": file_id,
                "busid": file_data.get("busid"),
                "name": (
                    file_data.get("name")
                    or file_data.get("filename")
                    or file_data.get("file_name")
                    or file_id
                ),
                "url": file_data.get("url") or "",
                "content_type": file_data.get("content_type") or "",
                "size": (
                    file_data.get("size")
                    or file_data.get("file_size")
                    or 0
                ),
            },
        }]
        digest = hashlib.sha256(
            f"{group_id}:{user_id}:{file_id}".encode("utf-8")
        ).hexdigest()
        event = ChatEvent(
            platform="onebot",
            channel="group",
            event_id=f"group-upload:{digest}",
            scope_id=group_id,
            sender_id=user_id,
            content="",
            attachments=self._attachments(segments),
        )
        if not await self.trigger_policy.should_handle(event):
            return {"status": "observed"}
        event_status = await asyncio.to_thread(
            self.store.get_event_status,
            event.event_key,
        )
        if event_status == "completed":
            return {"status": "handled"}
        resolved_segments = await self._resolve_group_file_urls(
            group_id,
            segments,
        )
        event = replace(
            event,
            attachments=self._attachments(resolved_segments),
        )
        await self.application.handle(event)
        return {"status": "handled"}

    async def _resolve_group_file_urls(
            self,
            group_id: str,
            segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved_segments = []
        for segment in segments:
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if segment.get("type") != "file" or data.get("url"):
                resolved_segments.append(segment)
                continue

            file_id = str(
                data.get("file_id")
                or data.get("id")
                or data.get("file")
                or ""
            ).strip()
            if not file_id:
                raise HTTPException(
                    status_code=400,
                    detail="missing group file id",
                )
            resolver = getattr(self.transport, "get_group_file_url", None)
            if resolver is None:
                raise HTTPException(
                    status_code=502,
                    detail="group file URL resolver is unavailable",
                )
            try:
                url = await resolver(group_id, file_id)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"failed to resolve group file URL: {exc}",
                ) from None
            resolved_segments.append({
                **segment,
                "data": {**data, "url": url},
            })
        return resolved_segments

    def _group_event(
            self,
            payload: dict[str, Any],
            segments: list[dict[str, Any]]) -> tuple[ChatEvent, bool]:
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
                    data.get("name")
                    or data.get("filename")
                    or data.get("file_name")
                    or data.get("file")
                    or "attachment"
                ),
                url=str(data.get("url") or ""),
                content_type=str(data.get("content_type") or ""),
                size=int(data.get("size") or data.get("file_size") or 0),
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
        raw_body = await request.body()
        authorization = request.headers.get("Authorization", "")
        inbound_header = request.headers.get("X-OneBot-Token", "")
        provided = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else inbound_header.strip()
        )
        token_authenticated = bool(provided) and secrets.compare_digest(
            provided,
            settings.inbound_token,
        )
        signature = request.headers.get("X-Signature", "").strip()
        expected_signature = "sha1=" + hmac.new(
            settings.inbound_token.encode("utf-8"),
            raw_body,
            hashlib.sha1,
        ).hexdigest()
        signature_authenticated = (
            bool(signature)
            and secrets.compare_digest(signature, expected_signature)
        )
        if not token_authenticated and not signature_authenticated:
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
