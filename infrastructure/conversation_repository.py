"""ConversationRepository: scoped storage, no business routing."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

RECENT_TURN_LIMIT = 6


@dataclass(frozen=True)
class ConversationTurn:
    user_content: str
    assistant_content: str
    created_at: str


@dataclass(frozen=True)
class ChatMessage:
    message_key: str
    platform: str
    group_id: str
    member_id: str
    message_id: str
    reply_to_id: str
    role: str
    content: str
    created_at: str


class ConversationRepository:
    def __init__(self, store):
        self.store = store

    def has_message(self, user_msg_id: str) -> bool:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_turns WHERE user_msg_id = ?",
                (user_msg_id,),
            ).fetchone()
        return row is not None

    def save_turn(
            self,
            group_openid: str,
            member_openid: str,
            user_msg_id: str,
            user_content: str,
            assistant_content: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_turns (
                    group_openid,
                    member_openid,
                    user_msg_id,
                    user_content,
                    assistant_content,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_openid,
                    member_openid,
                    user_msg_id,
                    user_content,
                    assistant_content,
                    created_at,
                ),
            )

    def get_recent_turns(
            self,
            group_openid: str,
            member_openid: str,
            limit: int = RECENT_TURN_LIMIT) -> tuple[ConversationTurn, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_content, assistant_content, created_at
                FROM conversation_turns
                WHERE group_openid = ? AND member_openid = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_openid, member_openid, limit),
            ).fetchall()

        return tuple(
            ConversationTurn(
                user_content=row["user_content"],
                assistant_content=row["assistant_content"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        )

    def save_chat_message(
            self,
            message_key: str,
            platform: str,
            group_id: str,
            member_id: str,
            message_id: str,
            reply_to_id: str,
            role: str,
            content: str,
            now: datetime | None = None) -> bool:
        created_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_messages (
                    message_key,
                    platform,
                    group_id,
                    member_id,
                    message_id,
                    reply_to_id,
                    role,
                    content,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_key,
                    platform,
                    group_id,
                    member_id,
                    message_id,
                    reply_to_id,
                    role,
                    content,
                    created_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def get_recent_chat_messages(
            self,
            platform: str,
            group_id: str,
            limit: int = 16,
            exclude_message_key: str = "") -> tuple[ChatMessage, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM chat_messages
                WHERE platform = ?
                  AND group_id = ?
                  AND message_key != ?
                ORDER BY created_at DESC, message_key DESC
                LIMIT ?
                """,
                (platform, group_id, exclude_message_key, limit),
            ).fetchall()
        return tuple(self._chat_message(row) for row in rows)

    def get_chat_message(
            self,
            platform: str,
            group_id: str,
            message_id: str) -> ChatMessage | None:
        if not message_id:
            return None
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM chat_messages
                WHERE platform = ?
                  AND group_id = ?
                  AND message_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (platform, group_id, message_id),
            ).fetchone()
        return self._chat_message(row) if row is not None else None

    def delete_chat_messages_before(self, cutoff: datetime) -> int:
        with self.store._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_messages WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
        return cursor.rowcount

    @staticmethod
    def _chat_message(row: sqlite3.Row) -> ChatMessage:
        return ChatMessage(
            message_key=str(row["message_key"]),
            platform=str(row["platform"]),
            group_id=str(row["group_id"]),
            member_id=str(row["member_id"]),
            message_id=str(row["message_id"]),
            reply_to_id=str(row["reply_to_id"] or ""),
            role=str(row["role"]),
            content=str(row["content"]),
            created_at=str(row["created_at"]),
        )

