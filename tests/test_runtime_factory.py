import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.runtime_factory import build_runtime
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from services.semantic_task_service import SemanticTaskService


class FakeTransport:
    async def send(self, message):
        return None


class FakeRenderer:
    def render(self, channel, command_content, reply_text):
        return {"content": reply_text}

    def render_reminder(self, recipient_id, text):
        return {"content": text}


class RuntimeFactoryTests(unittest.TestCase):
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
            self.assertIs(runtime.reservation_service.store, store)
            self.assertIs(
                runtime.tool_router.reservation_tools.service,
                runtime.reservation_service,
            )
            self.assertIs(
                runtime.tool_router.reservation_tools.draft_creator,
                runtime.reservation_draft_creator,
            )
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
            self.assertIsInstance(runtime.application.semantic_task_service, SemanticTaskService)
            self.assertEqual(runtime.application.semantic_task_service.mode, 'preview')
            runtime.application.semantic_task_service.compiler.client.close()


if __name__ == "__main__":
    unittest.main()
