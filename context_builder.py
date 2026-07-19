from __future__ import annotations

from dataclasses import dataclass

from chat_transport import ChatEvent
from memory_store import ConversationTurn, MemoryStore


MAX_CONTEXT_CHARS = 7000
MAX_QUOTED_CHARS = 800
MAX_RECENT_GROUP_CHARS = 2200
MAX_RECENT_GROUP_MESSAGES = 16
MAX_DOCUMENT_CHARS = 3200


@dataclass(frozen=True)
class AgentContext:
    recent_dialogue: tuple[ConversationTurn, ...]
    group_context: str
    document_context: str
    source_note: str

    @property
    def total_chars(self) -> int:
        dialogue_chars = sum(
            len(turn.user_content) + len(turn.assistant_content)
            for turn in self.recent_dialogue
        )
        return (
            dialogue_chars
            + len(self.group_context)
            + len(self.document_context)
            + len(self.source_note)
        )


class ContextBuilder:
    def __init__(self, store: MemoryStore):
        self.store = store

    def build(self, event: ChatEvent) -> AgentContext:
        quoted = self.store.get_chat_message(
            event.platform,
            event.scope_id,
            event.reply_to_id,
        )
        recent = self.store.get_recent_chat_messages(
            event.platform,
            event.scope_id,
            limit=MAX_RECENT_GROUP_MESSAGES,
            exclude_message_key=event.event_key,
        )
        group_context = self._group_context(quoted, recent)
        document_context = self.store.build_document_context(
            event.scope_id,
            event.content,
            max_chars=MAX_DOCUMENT_CHARS,
        )
        source_note = self._source_note(event.platform)
        remaining = max(
            0,
            MAX_CONTEXT_CHARS
            - len(group_context)
            - len(document_context)
            - len(source_note),
        )
        dialogue = self._trim_dialogue(
            self.store.get_recent_turns(
                event.scope_id,
                event.sender_id,
            ),
            remaining,
        )
        return AgentContext(
            recent_dialogue=dialogue,
            group_context=group_context,
            document_context=document_context,
            source_note=source_note,
        )

    @staticmethod
    def _group_context(quoted, recent) -> str:
        sections = []
        quoted_key = ""
        if quoted is not None:
            quoted_key = quoted.message_key
            quoted_line = (
                f"引用消息（成员 {quoted.member_id}）：{quoted.content}"
            )
            sections.append(ContextBuilder._clip(
                quoted_line,
                MAX_QUOTED_CHARS,
            ))

        selected = []
        used_chars = 0
        for message in recent:
            if message.message_key == quoted_key:
                continue
            line = f"成员 {message.member_id}：{message.content}"
            line_chars = len(line) + 1
            if used_chars + line_chars > MAX_RECENT_GROUP_CHARS:
                continue
            selected.append(line)
            used_chars += line_chars
        if selected:
            sections.append(
                "最近观察到的群消息：\n" + "\n".join(reversed(selected))
            )
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _trim_dialogue(
            turns: tuple[ConversationTurn, ...],
            budget: int) -> tuple[ConversationTurn, ...]:
        if budget <= 0:
            return ()
        selected = []
        used_chars = 0
        for turn in reversed(turns):
            turn_chars = len(turn.user_content) + len(turn.assistant_content)
            if used_chars + turn_chars > budget:
                continue
            selected.append(turn)
            used_chars += turn_chars
        return tuple(reversed(selected))

    @staticmethod
    def _source_note(platform: str) -> str:
        if platform == "qq_official":
            return (
                "QQ 官方 Bot 上下文仅包含机器人在线期间收到的部分群消息，"
                "不代表完整群聊记录。"
            )
        return "群聊上下文仅包含机器人在线期间观察到的消息。"

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[:max(0, limit - 1)] + "…"
