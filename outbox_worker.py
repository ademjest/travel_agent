from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

from chat_transport import MessageTransport, OutgoingMessage
from memory_store import MemoryStore


RETRY_SECONDS = (5, 15, 60, 300, 900)


def retry_delay(attempt_count: int) -> timedelta:
    index = min(max(attempt_count, 1), len(RETRY_SECONDS)) - 1
    return timedelta(seconds=RETRY_SECONDS[index])


class OutboxWorker:
    def __init__(
            self,
            platform: str,
            store: MemoryStore,
            transport: MessageTransport,
            clock: Callable[[], datetime] | None = None):
        self.platform = platform
        self.store = store
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def dispatch_due_once(self, now: datetime | None = None) -> int:
        listing_time = now or self.clock()
        delivered = 0
        rows = await asyncio.to_thread(
            self.store.list_due_outbox,
            self.platform,
            listing_time,
        )
        for row in rows:
            claim_time = now or self.clock()
            token = await asyncio.to_thread(
                self.store.claim_outbox,
                row.outbox_id,
                claim_time,
            )
            if token is None:
                continue
            try:
                await self.transport.send(OutgoingMessage(
                    channel=row.channel,
                    target_id=row.target_id,
                    reply_to_id=row.reply_to_id,
                    payload=row.payload,
                ))
            except Exception as exc:
                failed_at = now or self.clock()
                retry_at = failed_at + retry_delay(row.attempt_count + 1)
                await asyncio.to_thread(
                    self.store.mark_outbox_failed,
                    row.outbox_id,
                    token,
                    type(exc).__name__,
                    retry_at,
                )
            else:
                sent_at = now or self.clock()
                sent = await asyncio.to_thread(
                    self.store.mark_outbox_sent,
                    row.outbox_id,
                    token,
                    sent_at,
                )
                if sent:
                    delivered += 1
        return delivered

    async def run(self, poll_seconds: float = 5.0) -> None:
        while True:
            await self.dispatch_due_once()
            await asyncio.sleep(poll_seconds)
