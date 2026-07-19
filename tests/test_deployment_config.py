import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from memory_store import MemoryStore


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.compose_path = self.root / "deploy" / "napcat" / "compose.yml"

    def test_napcat_compose_uses_private_ports_and_persistent_volumes(self):
        compose = yaml.safe_load(self.compose_path.read_text(encoding="utf-8"))
        napcat = compose["services"]["napcat"]

        for port in napcat.get("ports", []):
            self.assertTrue(str(port).startswith("127.0.0.1:"))
        mounts = "\n".join(napcat["volumes"])
        self.assertIn("napcat_config", mounts)
        self.assertIn("qq_config", mounts)
        self.assertIn("napcat_config", compose["volumes"])
        self.assertIn("qq_config", compose["volumes"])

    def test_images_are_pinned_and_secrets_are_not_hardcoded(self):
        text = self.compose_path.read_text(encoding="utf-8")
        compose = yaml.safe_load(text)
        napcat_image = compose["services"]["napcat"]["image"]

        self.assertIn("@sha256:", napcat_image)
        self.assertNotIn(":latest", napcat_image)
        self.assertIn("${ONEBOT_ACCESS_TOKEN}", text)
        self.assertIn("${ONEBOT_INBOUND_TOKEN}", text)
        self.assertNotRegex(text, r"QQ_ACCOUNT\s*:\s*\d+")

    def test_retention_cleanup_only_deletes_old_chat_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
            store.save_chat_message(
                "old", "onebot", "group", "member", "old", "", "user",
                "旧消息", now=now - timedelta(days=31),
            )
            store.save_chat_message(
                "new", "onebot", "group", "member", "new", "", "user",
                "新消息", now=now - timedelta(days=1),
            )
            store.add_document(
                "group", "member", "plan.txt", "plan-hash",
                "住宿茶卡镇", ["住宿茶卡镇"],
            )

            deleted = store.delete_chat_messages_before(
                now - timedelta(days=30)
            )

            self.assertEqual(deleted, 1)
            messages = store.get_recent_chat_messages("onebot", "group")
            self.assertEqual([message.content for message in messages], ["新消息"])
            self.assertIn(
                "茶卡镇",
                store.build_document_context("group", "住宿"),
            )
