import asyncio
import os

import botpy
from botpy import logging
from botpy.message import C2CMessage, GroupMessage
from dotenv import load_dotenv

from bot_application import TravelBotApplication
from chat_transport import ChatAttachment, ChatEvent, OutgoingMessage
from document_service import DocumentService
from memory_store import MemoryStore
from outbox_worker import OutboxWorker
from qq_ui import build_group_message_payload
from settings import Settings, SettingsError
from travel_agent import TravelAgent
from travel_service import TravelService
from upload_binding import UploadBindingService


logger = logging.get_logger()


class QQOfficialTransport:
    def __init__(self, api):
        self.api = api

    async def send(self, message: OutgoingMessage) -> None:
        if message.channel == "group":
            await self.api.post_group_message(
                group_openid=message.target_id,
                msg_id=message.reply_to_id,
                msg_seq=1,
                **message.payload,
            )
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


class TravelRiskBot(botpy.Client):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(**kwargs)
        self.settings = settings
        self.travel_service = TravelService(settings)
        self.memory_store = MemoryStore()
        self.travel_agent = (
            TravelAgent(settings, self.travel_service.execute_tool)
            if settings.llm_configured
            else None
        )
        self.document_service = DocumentService(
            self.memory_store,
            summarizer=(
                self.travel_agent.summarize_document
                if self.travel_agent
                else None
            ),
        )
        self.upload_binding_service = UploadBindingService(
            self.memory_store,
            self.document_service,
            group_allowed=settings.allows_group,
        )
        self.reply_renderer = QQOfficialReplyRenderer()
        self.outbox_worker = OutboxWorker(
            "qq_official",
            self.memory_store,
            QQOfficialTransport(self.api),
        )
        self.application = TravelBotApplication(
            store=self.memory_store,
            travel_service=self.travel_service,
            travel_agent=self.travel_agent,
            document_service=self.document_service,
            upload_binding_service=self.upload_binding_service,
            outbox_worker=self.outbox_worker,
            reply_renderer=self.reply_renderer,
            group_allowed=settings.allows_group,
        )
        self._outbox_task = None

    async def on_ready(self):
        logger.info("Bot is online: %s", self.robot.name)
        logger.info("Memory database: %s", self.memory_store.database_path)
        logger.info(
            "Build: ref=%s sha=%s",
            os.getenv("APP_GIT_REF", "local"),
            os.getenv("APP_GIT_SHA", "unknown")[:12],
        )
        await self.outbox_worker.dispatch_due_once()
        outbox_task = getattr(self, "_outbox_task", None)
        if outbox_task is None or outbox_task.done():
            self._outbox_task = asyncio.create_task(
                self.outbox_worker.run(),
                name="qq-official-outbox",
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
            reply_to_id=message.id,
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
            reply_to_id=message.id,
            attachments=self._normalize_attachments(attachments),
        )
        await self.application.handle(event)

    @staticmethod
    def _normalize_attachments(attachments) -> tuple[ChatAttachment, ...]:
        return tuple(
            ChatAttachment(
                filename=str(getattr(item, "filename", "") or ""),
                url=str(getattr(item, "url", "") or ""),
                content_type=str(
                    getattr(item, "content_type", "") or ""
                ),
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
