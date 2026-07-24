from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from memory_store import MemoryStore


@dataclass(frozen=True)
class RetentionPolicy:
    chat_days: int = 30
    event_days: int = 30
    binding_days: int = 7
    reservation_days: int = 90
    image_days: int = 90
    document_days: int = 0

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        return cls(
            chat_days=_days("RETENTION_CHAT_DAYS", 30),
            event_days=_days("RETENTION_EVENT_DAYS", 30),
            binding_days=_days("RETENTION_BINDING_DAYS", 7),
            reservation_days=_days("RETENTION_RESERVATION_DAYS", 90),
            image_days=_days("RETENTION_IMAGE_DAYS", 90),
            document_days=_days("RETENTION_DOCUMENT_DAYS", 0),
        )


def _days(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 0 <= value <= 36_500:
        raise ValueError(f"{name} must be between 0 and 36500")
    return value


class MaintenanceService:
    def __init__(
            self,
            store: MemoryStore,
            image_root: str | Path,
            policy: RetentionPolicy | None = None,
            clock: Callable[[], datetime] | None = None):
        self.store = store
        self.image_root = Path(image_root).resolve()
        self.policy = policy or RetentionPolicy.from_env()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, object] = {}
        self.last_error = ""

    def run_once(self, now: datetime | None = None) -> dict[str, object]:
        current = now or self.clock()
        policy = self.policy
        result = self.store.purge_retained_data(
            chat_cutoff=current - timedelta(days=policy.chat_days),
            event_cutoff=current - timedelta(days=policy.event_days),
            binding_cutoff=current - timedelta(days=policy.binding_days),
            reservation_cutoff=(
                current - timedelta(days=policy.reservation_days)
            ),
            image_cutoff=current - timedelta(days=policy.image_days),
            document_cutoff=(
                current - timedelta(days=policy.document_days)
                if policy.document_days > 0
                else None
            ),
        )
        deleted_files = 0
        for raw_path in result.pop("image_paths", ()):
            path = Path(str(raw_path)).resolve()
            if not path.is_relative_to(self.image_root):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            deleted_files += 1
        result["image_files"] = deleted_files
        self.last_run_at = current
        self.last_result = result
        self.last_error = ""
        return result

    async def run(self, poll_seconds: float = 24 * 60 * 60) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise
            await asyncio.sleep(poll_seconds)
