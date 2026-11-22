"""Decide whether a group message addresses the bot; never authorize business tools."""
import asyncio

from core.commands import route_correction
from app.request_router import response_candidate, continuation_candidate, resume_weather_request
from tools.materials import MaterialsTool
from infrastructure.image_inputs import is_supported_attachment


class GroupTriggerPolicy:
    def __init__(self,store):
        self.store=store

    async def should_handle(self,event):
        if response_candidate(event.content):
            return True
        if any(MaterialsTool.is_document_attachment(attachment) for attachment in event.attachments):
            return True
        if any(is_supported_attachment(attachment) for attachment in event.attachments):
            return await asyncio.to_thread(self.store.reservations.reservation_workflow_is_active,
                event.platform,event.scope_id,event.sender_id)
        tasks=await asyncio.to_thread(self.store.tasks.recent,event.platform,event.storage_scope_id,event.sender_id,
            reply_to_id=event.reply_to_id,**({'include_failed_reads':True} if route_correction(event.content) else {}))
        if continuation_candidate(event.content,tasks):
            return True
        if await asyncio.to_thread(self.store.tasks.has_history,event.platform,event.storage_scope_id,event.sender_id):
            return False
        turns=await asyncio.to_thread(self.store.conversations.get_recent_turns,event.storage_scope_id,event.sender_id)
        return resume_weather_request(event.content,turns)!=event.content
