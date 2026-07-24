import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore
from runtime_factory import build_runtime
from settings import Settings


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
            self.assertIsNone(runtime.travel_agent)


if __name__ == "__main__":
    unittest.main()
