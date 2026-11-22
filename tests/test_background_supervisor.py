import asyncio
import unittest

from core.background_supervisor import BackgroundSupervisor


class BackgroundSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_resource_is_started_once_and_closed_with_supervisor(self):
        calls=[]
        class Optional:
            def start(self): calls.append('start')
            async def close(self): calls.append('close')
        supervisor=BackgroundSupervisor(resources=(Optional(),))
        stop=asyncio.Event()
        supervisor.start('a',stop.wait)
        supervisor.start('b',stop.wait)
        await supervisor.stop()
        self.assertEqual(calls,['start','close'])

    async def test_failed_task_is_restarted_and_reported(self):
        supervisor = BackgroundSupervisor(restart_delay_seconds=0.01)
        restarted = asyncio.Event()
        stop = asyncio.Event()
        calls = 0

        async def worker():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            restarted.set()
            await stop.wait()

        supervisor.start("worker", worker)
        await asyncio.wait_for(restarted.wait(), timeout=1)

        snapshot = supervisor.snapshot()["worker"]
        self.assertTrue(snapshot["running"])
        self.assertEqual(snapshot["restart_count"], 1)
        self.assertEqual(snapshot["last_error"], "RuntimeError")
        await supervisor.stop()

    async def test_start_does_not_duplicate_running_task(self):
        supervisor = BackgroundSupervisor()
        stop = asyncio.Event()

        async def worker():
            await stop.wait()

        first = supervisor.start("worker", worker)
        second = supervisor.start("worker", worker)

        self.assertIs(second, first)
        await supervisor.stop()


if __name__ == "__main__":
    unittest.main()
