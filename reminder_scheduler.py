from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Protocol

from memory_store import DueReservationReminder, MemoryStore
from reservation_service import BEIJING_TZ


class ReminderRenderer(Protocol):
    def render_reminder(
            self,
            recipient_id: str,
            text: str) -> dict[str, object]:
        raise RuntimeError("protocol method")


class ReminderScheduler:
    def __init__(
            self,
            platform: str,
            store: MemoryStore,
            renderer: ReminderRenderer,
            group_allowed: Callable[[str], bool],
            clock: Callable[[], datetime] | None = None):
        self.platform = platform
        self.store = store
        self.renderer = renderer
        self.group_allowed = group_allowed
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def scan_once(self, now: datetime | None = None) -> int:
        current = now or self.clock()
        rows = await asyncio.to_thread(
            self.store.list_due_reservation_reminders,
            self.platform,
            current,
        )
        queued = 0
        local_today = current.astimezone(BEIJING_TZ).date()
        for row in rows:
            if row.visit_date < local_today:
                await asyncio.to_thread(
                    self.store.mark_reservation_reminder_terminal,
                    row.reminder_id,
                    "expired",
                    "",
                    current,
                )
                continue
            if not self.group_allowed(row.group_id):
                await asyncio.to_thread(
                    self.store.mark_reservation_reminder_terminal,
                    row.reminder_id,
                    "blocked",
                    "group is not allowlisted",
                    current,
                )
                continue
            text = self._render_text(row, current)
            payload = self.renderer.render_reminder(
                row.recipient_id,
                text,
            )
            outbox_id = await asyncio.to_thread(
                self.store.queue_reservation_reminder,
                row.reminder_id,
                payload,
                text,
                current,
            )
            if outbox_id is not None:
                queued += 1
        return queued

    @staticmethod
    def _render_text(
            row: DueReservationReminder,
            now: datetime) -> str:
        lines = [
            f"景点预约提醒：{row.attraction_name}",
            f"游览日期：{row.visit_date.isoformat()}",
            f"建议预约日期：{row.booking_date.isoformat()}",
        ]
        if row.opening_hours:
            lines.append(f"开放时间：{row.opening_hours}")
        if row.price_text:
            lines.append(f"参考价格：{row.price_text}")
        if row.booking_channel:
            lines.append(f"预约渠道：{row.booking_channel}")
        else:
            lines.append("预约渠道：请前往景区官方渠道核对")
        if now > row.scheduled_at_utc:
            original = row.scheduled_at_utc.astimezone(
                BEIJING_TZ
            ).strftime("%Y-%m-%d %H:%M")
            lines.append(f"延迟补发：原定提醒时间 {original}")
        lines.append("预约政策可能变化，请以景区官方公告为准。")
        return "\n".join(lines)

    async def run(self, poll_seconds: float = 60.0) -> None:
        while True:
            await self.scan_once()
            await asyncio.sleep(poll_seconds)
