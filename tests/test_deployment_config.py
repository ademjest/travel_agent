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
        self.workflow_path = (
            self.root / ".github" / "workflows" / "scheduled-bot.yml"
        )

    def test_scheduled_workflow_installs_full_test_dependencies(self):
        workflow = self.workflow_path.read_text(encoding="utf-8")

        self.assertIn("pip install -r requirements-onebot.txt", workflow)

    def test_scheduled_workflow_diagnoses_qq_without_secrets(self):
        workflow = self.workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("# - name: Diagnose QQ connectivity", workflow)
        diagnostic = workflow.split(
            "- name: Diagnose QQ connectivity",
            maxsplit=1,
        )[1].split("- name: Run QQ Bot", maxsplit=1)[0]

        self.assertIn("getent ahosts bots.qq.com", diagnostic)
        self.assertIn("curl -4 -I", diagnostic)
        self.assertIn("curl -6 -I", diagnostic)
        self.assertIn("--connect-timeout 5", diagnostic)
        self.assertIn("--max-time 10", diagnostic)
        self.assertNotIn("QQ_BOT_APPID", diagnostic)
        self.assertNotIn("QQ_BOT_SECRET", diagnostic)

    def test_scheduled_workflow_retries_login_and_reports_heartbeat(self):
        workflow = self.workflow_path.read_text(encoding="utf-8")

        self.assertIn("max_attempts=3", workflow)
        self.assertIn(
            'for attempt in $(seq 1 "$max_attempts"); do',
            workflow,
        )
        self.assertIn("delay=$((attempt * 30))", workflow)
        self.assertIn("QQ Bot heartbeat", workflow)
        self.assertIn("sleep 60", workflow)
        self.assertIn('kill "$heartbeat_pid"', workflow)
        self.assertIn(
            'if [ "$status" -eq 124 ] || [ "$status" -eq 130 ]; then',
            workflow,
        )
        self.assertIn(
            "if: always() && steps.run_bot.outcome != 'skipped'",
            workflow,
        )

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
