import tempfile
from pathlib import Path
import unittest

from infrastructure.memory_store import MemoryStore
from tools.materials import MaterialsTool
from test_travel_agent import FakeClient, completion, assistant_message


class MaterialsToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name)/'materials.db')

    def test_document_summary_uses_plain_chat_completion(self):
        client = FakeClient([
            completion(assistant_message(content="旅行日期：8月16日。住宿：茶卡镇。"))
        ])
        tool = MaterialsTool(self.store, client=client, model='test')

        summary = tool.summarize_document(
            "plan.docx",
            "8月16日从西宁出发，当晚住宿茶卡镇。",
        )

        self.assertIn("住宿：茶卡镇", summary)
        request = client.completions.requests[0]
        self.assertNotIn("tools", request)
