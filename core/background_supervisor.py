from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone


logger = logging.getLogger(__name__)
CoroutineFactory = Callable[[], Awaitable[None]]


class BackgroundSupervisor:
    def __init__(self, restart_delay_seconds: float = 1.0):
        self.restart_delay_seconds = restart_delay_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._factories: dict[str, CoroutineFactory] = {}
        self._restart_counts: dict[str, int] = {}
        self._last_errors: dict[str, str] = {}
        self._last_failures: dict[str, str] = {}

    def start(self, name: str, factory: CoroutineFactory) -> asyncio.Task:
        current = self._tasks.get(name)
        if current is not None and not current.done():
            return current
        self._factories[name] = factory
        task = asyncio.create_task(self._run(name), name=name)
        self._tasks[name] = task
        return task

    async def _run(self, name: str) -> None:
        while True:
            try:
                await self._factories[name]()
                raise RuntimeError("background task stopped unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._restart_counts[name] = (
                    self._restart_counts.get(name, 0) + 1
                )
                self._last_errors[name] = type(exc).__name__
                self._last_failures[name] = datetime.now(
                    timezone.utc
                ).isoformat()
                logger.exception("Background task %s failed; restarting", name)
                await asyncio.sleep(self.restart_delay_seconds)

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "running": task is not None and not task.done(),
                "restart_count": self._restart_counts.get(name, 0),
                "last_error": self._last_errors.get(name, ""),
                "last_failure_at": self._last_failures.get(name, ""),
            }
            for name, task in self._tasks.items()
        }

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
