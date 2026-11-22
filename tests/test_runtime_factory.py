import asyncio
from contextlib import asynccontextmanager
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.runtime_factory import build_runtime
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from app.request_router import RequestRouter


class FakeTransport:
    async def send(self, message):
        return None


class FakeRenderer:
    def render(self, channel, command_content, reply_text):
        return {"content": reply_text}

    def render_reminder(self, recipient_id, text):
        return {"content": text}


class OptionalRailRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_rail_is_isolated_from_background_workers_and_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'memory.db')
            config=json.dumps({'enabled':True,'endpoint':'https://rail.example/mcp'})
            settings=Settings('','',frozenset(),'','','','',rail_mcp_config=config)
            runtime=build_runtime(settings,platform='web',store=store,transport=FakeTransport(),
                reply_renderer=FakeRenderer(),group_allowed=lambda _: True)
            @asynccontextmanager
            async def unavailable(config):
                raise ConnectionError('credential-sensitive provider detail')
                yield
            runtime.rail_client.mcp.session_factory=unavailable
            stop=asyncio.Event()
            runtime.supervisor.start('existing-worker',stop.wait)
            await runtime.rail_client.mcp._ready.wait()
            self.assertEqual(runtime.rail_client.mcp.status()['state'],'unavailable')
            self.assertTrue(runtime.supervisor.snapshot()['existing-worker']['running'])
            self.assertEqual(runtime.tool_registry.definitions['query_train_tickets'].owner,'travel_query')
            await runtime.supervisor.stop()


class RuntimeFactoryTests(unittest.TestCase):
    def test_invalid_optional_rail_config_cannot_break_runtime_or_expand_tool_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'memory.db')
            settings=Settings('','',frozenset(),'','','','',rail_mcp_config='invalid')
            runtime=build_runtime(settings,platform='web',store=store,transport=FakeTransport(),
                reply_renderer=FakeRenderer(),group_allowed=lambda _: True)
            self.assertIsNone(runtime.rail_client)
            self.assertEqual(runtime.tool_registry.definitions['query_train_tickets'].owner,'travel_query')

    def test_shared_runtime_wires_one_store_and_reservation_tool_router(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            settings = Settings(
                appid="",
                secret="",
                allowed_group_openids=frozenset({"group-a"}),
                amap_api_key="",
                llm_api_key="",
                llm_base_url="",
                llm_model_id="",
            )

            runtime = build_runtime(
                settings,
                platform="onebot",
                transport=FakeTransport(),
                reply_renderer=FakeRenderer(),
                group_allowed=lambda group_id: group_id == "group-a",
                store=store,
            )

            self.assertIs(runtime.application.store, store)
            self.assertIs(runtime.application.personal_reminder_service, runtime.application.reminders_tool)
            self.assertIs(runtime.application.scheduled_query_service, runtime.application.reminders_tool)
            self.assertIs(runtime.application.policy_watch_service, runtime.application.reminders_tool)
            self.assertIs(runtime.application.booking_reminder_service, runtime.application.reminders_tool)
            self.assertIs(runtime.application.image_context_service, runtime.document_service)
            self.assertIs(runtime.application.read_only_service, runtime.travel_service)
            self.assertIs(runtime.tool_router.travel_service, runtime.travel_service)
            self.assertIs(runtime.travel_service.tool_executor.__self__, runtime.tool_router)
            self.assertIs(runtime.tool_registry, runtime.application.tool_router.registry)
            self.assertEqual(len(runtime.tool_registry.definitions), 26)
            self.assertIs(runtime.tool_registry.definitions['query_official_website'].handler.__self__,
                          runtime.application.research_service.search_tool)
            self.assertIs(runtime.reservation_service.store, store)
            self.assertIs(runtime.tool_router.reservation_tools, runtime.reservation_service)
            self.assertIs(runtime.reservation_draft_creator, runtime.reservation_service)
            self.assertIs(runtime.reservation_service.image_service, runtime.reservation_image_service)
            self.assertEqual(
                runtime.reservation_image_service.image_root,
                store.database_path.parent / "images",
            )
            self.assertIsNone(runtime.travel_agent)

    def test_configured_model_wires_semantic_compiler_with_rollout_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict('os.environ', {'TRAVEL_SEMANTIC_MODE': 'preview'}):
            runtime = build_runtime(Settings('', '', frozenset({'group-a'}), '', 'key', 'https://example.test/v1', 'model'),
                platform='onebot', transport=FakeTransport(), reply_renderer=FakeRenderer(),
                group_allowed=lambda group_id: group_id == 'group-a', store=MemoryStore(Path(temp_dir) / 'memory.db'))
            self.assertIsInstance(runtime.application.semantic_task_service, RequestRouter)
            self.assertEqual(runtime.application.semantic_task_service.mode, 'preview')
            runtime.application.semantic_task_service.compiler.client.close()


if __name__ == "__main__":
    unittest.main()
