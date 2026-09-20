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
from datetime import datetime, timezone
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
from services.inbox_worker import InboxWorker
from infrastructure.model_gateway import usage_summary


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

    async def send(self, message: OutgoingMessage) -> str | None:
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
        message_id = (result.get('data') or {}).get('message_id')
        return str(message_id) if message_id is not None else None

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
    platform = 'onebot'

    def input_text(self, payload):
        return self._text_content(payload, self._segments(payload))

    def input_event(self, payload):
        return (self._group_event(payload, self._segments(payload))[0]
                if payload.get('group_id') else self._private_event(payload))

    @staticmethod
    def input_channel(payload):
        return 'group' if payload.get('group_id') else 'private'

    @staticmethod
    def input_reply_id(payload):
        return str(payload.get('message_id') or '')

    def scope_allowed(self, scope):
        return self.settings.allows_group(scope)

    async def capture_failure_actionable(self, payload):
        if payload.get('message_type') == 'private':
            return True
        if payload.get('post_type') == 'notice':
            filename = str(payload.get('file', {}).get('name') or payload.get('file', {}).get('filename') or '')
            return Path(filename).suffix.lower() in {'.docx', '.txt', '.md', '.xlsx', '.doc', '.xls'}
        event, direct = self._group_event(payload, self._segments(payload))
        return direct or await self.trigger_policy.should_handle(event)

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
        self.attachment_cache = None

    def normalize_for_inbox(self, payload):
        post_type = payload.get('post_type')
        if post_type not in {'message', 'notice'}:
            return None
        if post_type == 'notice' and payload.get('notice_type') != 'group_upload':
            return None
        if post_type == 'message' and payload.get('message_type') not in {'group', 'private'}:
            return None
        user = self._required_id(payload, 'user_id')
        self_id = self._required_id(payload, 'self_id')
        if user == self_id:
            return None
        is_upload = post_type == 'notice' and payload.get('notice_type') == 'group_upload'
        if post_type == 'notice' and not is_upload:
            return None
        channel = 'group' if is_upload else payload.get('message_type')
        if channel not in {'group', 'private'}:
            return None
        group = self._required_id(payload, 'group_id') if channel == 'group' else user
        if channel == 'group' and not self.settings.allows_group(group):
            raise HTTPException(status_code=403, detail='group not allowed')
        self._event_time(payload)
        clean = {'post_type': post_type, 'user_id': user, 'self_id': self_id}
        clean['time'] = payload['time'] if payload.get('time') is not None else datetime.now(timezone.utc).timestamp()
        if channel == 'group':
            clean['group_id'] = group
        if is_upload:
            file_data = payload.get('file')
            if not isinstance(file_data, dict):
                raise HTTPException(status_code=400, detail='missing group upload file')
            clean['notice_type'] = 'group_upload'
            clean['file'] = self._clean_segment_data(file_data)
            file_id = str(clean['file'].get('file_id') or clean['file'].get('id') or clean['file'].get('file') or '')
            if not file_id:
                raise HTTPException(status_code=400, detail='missing group file id')
            digest = hashlib.sha256(f'{group}:{user}:{file_id}'.encode()).hexdigest()
            event_id = f'group-upload:{digest}'
            has_assets = True
        else:
            event_id = self._required_id(payload, 'message_id')
            clean.update(message_type=channel, message_id=event_id)
            segments = self._segments(payload)
            if len(segments) > 64:
                raise HTTPException(status_code=400, detail='too many message segments')
            cleaned = []
            for segment in segments:
                kind = segment.get('type')
                if kind not in {'text', 'at', 'reply', 'image', 'file'}:
                    continue
                data = self._clean_segment_data(segment.get('data') or {})
                if kind == 'image':
                    data.setdefault('content_type', 'image/unknown')
                cleaned.append({'type': kind, 'data': data})
            clean['message'] = cleaned
            if not cleaned:
                clean['raw_message'] = self._text_content(payload, [])[:8001]
            if payload.get('reply_to_bot') is True:
                clean['reply_to_bot'] = True
            if sum(item['type'] in {'image', 'file'} for item in cleaned) > 8:
                raise HTTPException(status_code=400, detail='too many attachments')
            has_assets = any(item['type'] in {'image', 'file'} for item in cleaned)
        return f'onebot:{channel}:{group}:{event_id}', group, user, clean, has_assets

    @staticmethod
    def _clean_segment_data(data):
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail='invalid segment data')
        result = {}
        for key in ('text', 'qq', 'user_id', 'id', 'file_id', 'file', 'name', 'filename', 'file_name', 'url', 'content_type', 'busid'):
            if data.get(key) is not None:
                value = str(data[key])
                limit = 8001 if key == 'text' else 4096 if key == 'url' else 512
                if len(value) > limit:
                    raise HTTPException(status_code=400, detail=f'segment field too long: {key}')
                result[key] = value
        for key in ('size', 'file_size'):
            if data.get(key) is not None:
                try:
                    value = int(data[key])
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail='invalid attachment size') from None
                if value < 0:
                    raise HTTPException(status_code=400, detail='invalid attachment size')
                result[key] = value
        return result

    async def event_for_capture(self, payload):
        if payload.get('post_type') == 'notice':
            file_data = payload['file']
            file_id = str(file_data.get('file_id') or file_data.get('id') or file_data.get('file'))
            group, user = str(payload['group_id']), str(payload['user_id'])
            digest = hashlib.sha256(f'{group}:{user}:{file_id}'.encode()).hexdigest()
            segments = [{'type': 'file', 'data': {**file_data, 'file_id': file_id}}]
            segments = await self._resolve_group_file_urls(group, segments)
            return ChatEvent('onebot', 'group', f'group-upload:{digest}', group, user, '', attachments=self._attachments(segments))
        if payload['message_type'] == 'private':
            return self._private_event(payload)
        segments = await self._resolve_group_file_urls(str(payload['group_id']), self._segments(payload))
        return self._group_event(payload, segments)[0]

    async def _hydrate(self, event):
        return await asyncio.to_thread(self.attachment_cache.hydrate, event) if self.attachment_cache else event

    async def _captured(self, event_key):
        return await asyncio.to_thread(self.attachment_cache.captured, event_key) if self.attachment_cache else False

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
                await self.application.handle(await self._hydrate(self._private_event(payload)))
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
        event = await self._hydrate(event)
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
            if await self._captured(event.event_key):
                await self.application.handle(event)
                return {'status': 'handled'}
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
        event = await self._hydrate(event)
        if not await self.trigger_policy.should_handle(event):
            return {"status": "observed"}
        event_status = await asyncio.to_thread(
            self.store.get_event_status,
            event.event_key,
        )
        if event_status == "completed":
            return {"status": "handled"}
        if await self._captured(event.event_key):
            await self.application.handle(event)
            return {'status': 'handled'}
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
            occurred_at=self._event_time(payload),
        ), mentioned or reply_to_bot

    @staticmethod
    def _event_time(payload):
        timestamp = payload.get('time')
        if timestamp is None:
            return None
        if type(timestamp) not in {int, float}:
            raise HTTPException(status_code=400, detail='invalid event time')
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OSError, OverflowError):
            raise HTTPException(status_code=400, detail='invalid event time') from None

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
        supervisor: BackgroundSupervisor | None = None,
        inbox_factory=InboxWorker) -> FastAPI:
    adapter = OneBotAdapter(settings, application, store)
    inbox = inbox_factory(store, adapter)
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
        task_supervisor.start('onebot-inbox', inbox.run)
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
            model_client = getattr(getattr(application, 'travel_agent', None), 'client', None)
            close_model = getattr(model_client, 'close', None)
            if close_model is not None:
                await asyncio.to_thread(close_model)

    app = FastAPI(lifespan=lifespan)
    app.state.inbox_worker = inbox

    @app.post("/onebot")
    async def onebot_endpoint(request: Request):
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > 128 * 1024:
                raise HTTPException(status_code=413, detail='event body too large')
            chunks.append(chunk)
        raw_body = b''.join(chunks)
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
        try:
            import json
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail='invalid JSON event') from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid event")
        return await inbox.submit(payload)

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
                "onebot-maintenance", 'onebot-inbox'):
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
            'inbox': await asyncio.to_thread(store.inbox.health),
            'model_usage': await asyncio.to_thread(usage_summary, store),
            'semantic_compiler': {
                'enabled': getattr(application, 'semantic_task_service', None) is not None,
                'mode': getattr(getattr(application, 'semantic_task_service', None), 'mode', 'disabled'),
            },
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
