import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_transport import ChatEvent
from context_builder import MAX_CONTEXT_CHARS, ContextBuilder
from memory_store import MemoryStore


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.db")
        self.builder = ContextBuilder(self.store)
        self.now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def save_message(
            self,
            message_id,
            content,
            *,
            group_id="group-a",
            member_id="member-a",
            seconds=0):
        self.store.save_chat_message(
            message_key=f"qq_official:group:{group_id}:{message_id}",
            platform="qq_official",
            group_id=group_id,
            member_id=member_id,
            message_id=message_id,
            reply_to_id="",
            role="user",
            content=content,
            now=self.now + timedelta(seconds=seconds),
        )

    @staticmethod
    def event(content="当前问题", reply_to_id=""):
        return ChatEvent(
            platform="qq_official",
            channel="group",
            event_id="current-message",
            scope_id="group-a",
            sender_id="member-current",
            content=content,
            reply_to_id=reply_to_id,
        )

    def test_group_context_is_isolated(self):
        self.save_message("a1", "青海湖集合", group_id="group-a")
        self.save_message("b1", "不应出现的群消息", group_id="group-b")

        context = self.builder.build(self.event())

        self.assertIn("青海湖集合", context.group_context)
        self.assertNotIn("不应出现的群消息", context.group_context)

    def test_speaker_attribution_is_preserved(self):
        self.save_message(
            "a1",
            "我负责预订茶卡住宿",
            member_id="member-planner",
        )

        context = self.builder.build(self.event())

        self.assertIn("member-planner", context.group_context)
        self.assertIn("我负责预订茶卡住宿", context.group_context)

    def test_quoted_message_precedes_recent_group_context(self):
        self.save_message("quoted", "引用的集合时间", seconds=1)
        self.save_message("recent", "普通最近消息", seconds=2)

        context = self.builder.build(
            self.event(reply_to_id="quoted")
        )

        self.assertLess(
            context.group_context.index("引用的集合时间"),
            context.group_context.index("普通最近消息"),
        )

    def test_newest_messages_have_priority_within_budget(self):
        self.save_message("old", "旧消息" + "甲" * 2100, seconds=1)
        self.save_message("new", "新消息" + "乙" * 1200, seconds=2)

        context = self.builder.build(self.event())

        self.assertIn("新消息", context.group_context)
        self.assertNotIn("旧消息", context.group_context)

    def test_context_snapshot_never_exceeds_total_budget(self):
        for index in range(20):
            self.save_message(
                f"message-{index}",
                f"消息{index}" + "甲" * 500,
                member_id=f"member-{index}",
                seconds=index,
            )
        self.store.add_document(
            "group-a",
            "member-a",
            "plan.txt",
            "context-budget-doc",
            "茶卡住宿" + "乙" * 5000,
            ["茶卡住宿" + "乙" * 5000],
        )

        context = self.builder.build(self.event("茶卡住宿怎么安排"))

        self.assertLessEqual(context.total_chars, MAX_CONTEXT_CHARS)

    def test_document_context_is_not_displaced_by_unrelated_chat(self):
        for index in range(20):
            self.save_message(
                f"unrelated-{index}",
                "与住宿无关的闲聊" + "甲" * 300,
                seconds=index,
            )
        self.store.add_document(
            "group-a",
            "member-a",
            "plan.txt",
            "document-priority",
            "8月16日晚住宿茶卡镇。",
            ["8月16日晚住宿茶卡镇。"],
        )

        context = self.builder.build(self.event("我们住哪里"))

        self.assertIn("茶卡镇", context.document_context)

    def test_official_context_is_labeled_as_partial_observation(self):
        context = self.builder.build(self.event())

        self.assertIn("部分群消息", context.source_note)
        self.assertIn("QQ 官方", context.source_note)
