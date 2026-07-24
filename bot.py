import asyncio
import os

import botpy
from botpy import logging
from botpy.message import C2CMessage, GroupMessage
from dotenv import load_dotenv

from background_supervisor import BackgroundSupervisor
from chat_transport import ChatAttachment, ChatEvent, OutgoingMessage
from qq_ui import build_group_message_payload
from runtime_factory import build_runtime
from settings import Settings, SettingsError


logger = logging.get_logger()


class QQOfficialTransport:
    def __init__(self, api):
        self.api = api

    async def send(self, message: OutgoingMessage) -> None:
        if message.channel == "group":
            parameters = {
                "group_openid": message.target_id,
                "msg_seq": 1,
                **message.payload,
            }
            if message.reply_to_id:
                parameters["msg_id"] = message.reply_to_id
            await self.api.post_group_message(**parameters)
            return
        await self.api.post_c2c_message(
            openid=message.target_id,
            msg_id=message.reply_to_id,
            msg_seq=1,
            **message.payload,
        )


class QQOfficialReplyRenderer:
    def render(self, channel, command_content, reply_text):
        if channel == "group":
            return build_group_message_payload(command_content, reply_text)
        return {"msg_type": 0, "content": reply_text}

    def render_reminder(self, recipient_id: str, text: str):
        mention = f"<@!{recipient_id}> " if recipient_id else ""
        return {
            "msg_type": 0,
            "content": mention + text,
        }


class TravelRiskBot(botpy.Client):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(**kwargs)
        self.settings = settings
        self.reply_renderer = QQOfficialReplyRenderer()
        runtime = build_runtime(
            settings,
            platform="qq_official",
            transport=QQOfficialTransport(self.api),
            reply_renderer=self.reply_renderer,
            group_allowed=settings.allows_group,
        )
        self.memory_store = runtime.store
        self.travel_service = runtime.travel_service
        self.reservation_service = runtime.reservation_service
        self.agent_tool_router = runtime.tool_router
        self.travel_agent = runtime.travel_agent
        self.document_service = runtime.document_service
        self.upload_binding_service = runtime.upload_binding_service
        self.image_extractor = runtime.image_extractor
        self.reservation_image_service = runtime.reservation_image_service
        self.outbox_worker = runtime.outbox_worker
        self.reminder_scheduler = runtime.reminder_scheduler
        self.maintenance_service = runtime.maintenance_service
        self.application = runtime.application
        self.background_supervisor = runtime.supervisor
        self._outbox_task = None
        self._reminder_task = None
        self._maintenance_task = None

    async def on_ready(self):
        maintenance = getattr(self, "maintenance_service", None)
        maintenance_result = (
            await asyncio.to_thread(maintenance.run_once)
            if maintenance is not None
            else {}
        )
        logger.info("Bot is online: %s", self.robot.name)
        logger.info("Memory database: %s", self.memory_store.database_path)
        logger.info("Retention cleanup: %s", maintenance_result)
        logger.info(
            "Build: ref=%s sha=%s",
            os.getenv("APP_GIT_REF", "local"),
            os.getenv("APP_GIT_SHA", "unknown")[:12],
        )
        await self.reminder_scheduler.scan_once()
        await self.outbox_worker.dispatch_due_once()
        supervisor = getattr(self, "background_supervisor", None)
        if supervisor is None:
            supervisor = BackgroundSupervisor()
            self.background_supervisor = supervisor
        self._outbox_task = supervisor.start(
            "qq-official-outbox",
            self.outbox_worker.run,
        )
        self._reminder_task = supervisor.start(
            "qq-official-reservation-reminders",
            self.reminder_scheduler.run,
        )
        if maintenance is not None:
            self._maintenance_task = supervisor.start(
                "qq-official-maintenance",
                maintenance.run,
            )

    async def on_group_at_message_create(self, message: GroupMessage):
        group_openid = message.group_openid
        logger.info(
            "Received group message: group_openid=%s msg_id=%s attachments=%s",
            group_openid,
            message.id,
            len(message.attachments or []),
        )

        if not message.id:
            logger.warning("Ignored group message without msg_id")
            return
        event = ChatEvent(
            platform="qq_official",
            channel="group",
            event_id=message.id,
            scope_id=group_openid,
            sender_id=str(
                getattr(message.author, "member_openid", "") or "unknown"
            ),
            content=(message.content or "").strip(),
            reply_to_id=self._quoted_message_id(message),
            attachments=self._normalize_attachments(message.attachments),
        )
        await self.application.handle(event)

    async def on_c2c_message_create(self, message: C2CMessage):
        user_openid = str(
            getattr(message.author, "user_openid", "") or ""
        )
        attachments = list(message.attachments or [])
        logger.info(
            "Received private message: user_openid=%s msg_id=%s attachments=%s",
            user_openid,
            message.id,
            len(attachments),
        )
        if not user_openid:
            logger.warning("Ignored private message without user_openid")
            return
        if not message.id:
            logger.warning("Ignored private message without msg_id")
            return
        event = ChatEvent(
            platform="qq_official",
            channel="private",
            event_id=message.id,
            scope_id=user_openid,
            sender_id=user_openid,
            content=(message.content or "").strip(),
            attachments=self._normalize_attachments(attachments),
        )
        await self.application.handle(event)

    @staticmethod
    def _quoted_message_id(message) -> str:
        reference = getattr(message, "message_reference", None)
        return str(
            getattr(reference, "message_id", "")
            or getattr(message, "reply_to_id", "")
            or ""
        )

    @staticmethod
    def _normalize_attachments(attachments) -> tuple[ChatAttachment, ...]:
        return tuple(
            ChatAttachment(
                filename=str(getattr(item, "filename", "") or ""),
                url=str(getattr(item, "url", "") or ""),
                content_type=str(
                    getattr(item, "content_type", "") or ""
                ),
                size=int(getattr(item, "size", 0) or 0),
            )
            for item in (attachments or [])
        )


def main() -> None:
    load_dotenv()

    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc

    intents = botpy.Intents(public_messages=True)
    client = TravelRiskBot(settings=settings, intents=intents)
    client.run(appid=settings.appid, secret=settings.secret)


if __name__ == "__main__":
    main()
