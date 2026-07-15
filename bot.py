import asyncio

import botpy
from botpy import logging
from botpy.message import C2CMessage, GroupMessage
from dotenv import load_dotenv
from openai import OpenAIError

from commands import parse_command
from document_service import DocumentService
from memory_store import MemoryStore
from settings import Settings, SettingsError
from travel_agent import TravelAgent
from travel_service import TravelService
from upload_binding import UploadBindingService


logger = logging.get_logger()


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

    async def on_ready(self):
        logger.info("Bot is online: %s", self.robot.name)
        logger.info("Memory database: %s", self.memory_store.database_path)

    async def on_group_at_message_create(self, message: GroupMessage):
        group_openid = message.group_openid
        logger.info(
            "Received group message: group_openid=%s msg_id=%s attachments=%s",
            group_openid,
            message.id,
            len(message.attachments or []),
        )

        if not self.settings.allows_group(group_openid):
            logger.warning("Ignored message from a group outside the allowlist")
            return

        member_openid = str(
            getattr(message.author, "member_openid", "") or "unknown"
        )
        if message.id and not await asyncio.to_thread(
                self.memory_store.claim_event,
                message.id):
            logger.info("Ignored duplicate message: msg_id=%s", message.id)
            return

        content = (message.content or "").strip()
        memory_content = content
        try:
            document_result = await asyncio.to_thread(
                self.document_service.ingest_attachments,
                group_openid,
                member_openid,
                list(message.attachments or []),
            )
            if document_result.handled:
                reply = document_result.reply
                memory_content = (
                    document_result.memory_content
                    or memory_content
                    or "上传旅行文档"
                )
            else:
                command = parse_command(content)
                if command.name == "upload_document":
                    reply = await asyncio.to_thread(
                        self.upload_binding_service.issue_binding,
                        group_openid,
                        member_openid,
                    )
                elif command.name != "unknown" or not self.travel_agent:
                    reply = await asyncio.to_thread(
                        self.travel_service.handle,
                        content,
                    )
                else:
                    history = await asyncio.to_thread(
                        self.memory_store.get_recent_turns,
                        group_openid,
                        member_openid,
                    )
                    knowledge_context = await asyncio.to_thread(
                        self.memory_store.build_document_context,
                        group_openid,
                        content,
                    )
                    agent_result = await asyncio.to_thread(
                        self.travel_agent.run,
                        content,
                        history,
                        knowledge_context,
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

        await message._api.post_group_message(
            group_openid=group_openid,
            msg_type=0,
            msg_id=message.id,
            msg_seq=1,
            content=reply,
        )
        await asyncio.to_thread(
            self.memory_store.save_turn,
            group_openid,
            member_openid,
            message.id,
            memory_content or "空消息",
            reply,
        )

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
        if message.id and not await asyncio.to_thread(
                self.memory_store.claim_event,
                message.id):
            logger.info("Ignored duplicate private message: msg_id=%s", message.id)
            return

        try:
            result = await asyncio.to_thread(
                self.upload_binding_service.handle_private_message,
                user_openid,
                (message.content or "").strip(),
                attachments,
            )
            reply = result.reply
        except Exception:
            logger.exception("Unexpected error while handling private message")
            reply = "处理私聊文件时出现内部错误，请稍后重试。"

        await message._api.post_c2c_message(
            openid=user_openid,
            msg_type=0,
            msg_id=message.id,
            msg_seq=1,
            content=reply,
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
