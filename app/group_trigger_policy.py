from __future__ import annotations

import asyncio

from agents.travel_decision import decide_travel_action
from core.chat_transport import ChatEvent
from core.commands import parse_command
from infrastructure.memory_store import MemoryStore
from services.document_service import DocumentService
from services.vision_service import ReservationImageService


class GroupTriggerPolicy:
    def __init__(self, store: MemoryStore):
        self.store = store

    async def should_handle(self, event: ChatEvent) -> bool:
        if parse_command(event.content).name != "unknown":
            return True

        decision = decide_travel_action(event.content)
        if decision.intent != "general":
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
            return False

        workflow_active = await asyncio.to_thread(
            self.store.reservation_workflow_is_active,
            event.platform,
            event.scope_id,
            event.sender_id,
        )
        if workflow_active:
            return True

        return "reservation" in decision.intents
