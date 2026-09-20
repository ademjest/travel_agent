from __future__ import annotations

import asyncio
import re

from agents.clarification import resume_weather_request
from agents.travel_decision import decide_travel_action
from core.chat_transport import ChatEvent
from core.tasks import read_task_continuation
from core.commands import parse_command
from infrastructure.memory_store import MemoryStore
from services.document_service import DocumentService
from services.vision_service import ReservationImageService
from services.personal_reminder_service import reminder_request
from services.booking_reminder_service import booking_continuation
from services.trip_service import trip_request, trip_continuation
from services.scheduled_query_service import scheduled_query_request, scheduled_query_control, scheduled_query_continuation
from services.policy_watch_service import watch_request, watch_control, watch_continuation
from services.preference_service import preference_request
from services.research_service import research_request, research_continuation


class GroupTriggerPolicy:
    def __init__(self, store: MemoryStore):
        self.store = store

    async def should_handle(self, event: ChatEvent) -> bool:
        if preference_request(event.content) or research_request(event.content):
            return True
        if watch_request(event.content) or watch_control(event.content):
            return True
        if scheduled_query_request(event.content) or scheduled_query_control(event.content):
            return True
        if trip_request(event.content):
            return True
        if event.content.strip() in {'查看任务', '查看任务进度', '我的任务', '取消当前任务', '退出当前任务'}:
            return True
        if reminder_request(event.content):
            return True
        if parse_command(event.content).name != "unknown":
            return True

        if re.fullmatch(r'(?:今天|明天)?(?:早饭|午饭|晚饭|早餐|午餐|晚餐)吃什么(?:呀|啊|呢)?[？?。 ]*', event.content.strip()):
            return False

        decision = decide_travel_action(event.content)
        explicit_request = re.search(
            r"帮我|帮忙|请|查询|查看|刷新|确认|取消|修改|补充|新增|制定|列出|根据|按这张|"
            r"推荐|找一家|找一下|安排|规划|日期补为|日期改为|不去参观|怎么|如何|多少|什么|哪里|哪[个天里家]|[？?]|吗\s*$",
            event.content,
        )
        if decision.intent != "general" and explicit_request:
            return True

        if any(
                DocumentService.is_document_attachment(attachment)
                for attachment in event.attachments):
            return True

        has_image = any(
            ReservationImageService.is_supported_attachment(attachment)
            for attachment in event.attachments
        )
        if not has_image:
            tasks = await asyncio.to_thread(
                self.store.tasks.recent, event.platform, event.storage_scope_id, event.sender_id,
                reply_to_id=event.reply_to_id)
            # Task ownership, not a vocabulary list, determines who can answer a pending reminder.
            if any(task.task_type == 'reminder' and task.status == 'collecting' for task in tasks):
                return bool(event.content.strip()) and len(event.content) <= 8000
            task, _ = read_task_continuation(event.content, tasks)
            if (task is not None
                    or booking_continuation(event.content, tasks) is not None
                    or trip_continuation(event.content, tasks) is not None
                    or scheduled_query_continuation(event.content, tasks) is not None
                    or watch_continuation(event.content, tasks) is not None
                    or research_continuation(event.content, tasks) is not None):
                return True
            if sum(task.status == 'collecting' and task.task_type == 'semantic' for task in tasks) == 1:
                return len(event.content.strip()) <= 200
            if await asyncio.to_thread(self.store.tasks.has_history, event.platform, event.storage_scope_id, event.sender_id):
                return False
            turns = await asyncio.to_thread(
                self.store.get_recent_turns, event.storage_scope_id, event.sender_id)
            return resume_weather_request(event.content, turns) != event.content

        workflow_active = await asyncio.to_thread(
            self.store.reservation_workflow_is_active,
            event.platform,
            event.scope_id,
            event.sender_id,
        )
        if workflow_active:
            return True

        return "reservation" in decision.intents
