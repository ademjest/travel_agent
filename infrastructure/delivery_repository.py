"""Event claims, operation checkpoints and transactional Outbox delivery."""
from __future__ import annotations
import json
import sqlite3
import uuid
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from core.chat_transport import storage_scope_id
from core.tool_result import LEGACY_CATALOG, COMPACT_CATALOG, CATALOG_VERSIONS

EVENT_PROCESSING_LEASE = timedelta(minutes=10)
MAX_EVENT_ERROR_CHARS = 1000


@dataclass(frozen=True)
class EventClaim:
    event_id: str
    claim_token: str
    prepared_reply: str | None = None
    prepared_memory_content: str | None = None


@dataclass(frozen=True)
class OutboxMessage:
    outbox_id: int
    event_id: str
    platform: str
    channel: str
    target_id: str
    sender_id: str
    reply_to_id: str
    payload: dict[str, object]
    attempt_count: int



class DeliveryRepository:
    def __init__(self,store):
        self.store=store

    def freeze_skill_assets(self, event_id, assets):
        """Bind existing operation checkpoints to assets; changed/missing old bundles stop before submission."""
        from infrastructure.skill_manager import SkillVersionUnavailable
        key = 'skills:' + hashlib.sha256(event_id.encode()).hexdigest()
        requested = json.dumps(assets, sort_keys=True, separators=(',', ':'))
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM processed_events WHERE event_id=?', (event_id,)).fetchone():
                return  # Direct technical/evaluation calls have no business checkpoint to resume.
            saved = db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?', (key,)).fetchone()
            if saved:
                if saved['result_text'] != requested:
                    raise SkillVersionUnavailable('任务绑定的旧提示资产不可用，已停止模型续传；请发起新请求。')
                return
            legacy = db.execute("SELECT 1 FROM model_calls WHERE event_key=? AND stage_id='' "
                "AND purpose NOT IN ('service_probe','retrieval_embedding') LIMIT 1", (event_id,)).fetchone()
            if legacy:
                raise SkillVersionUnavailable('历史任务未绑定提示资产版本，已停止模型续传；请发起新请求。')
            db.execute('''INSERT INTO event_tool_results
                (operation_key,event_id,tool_name,arguments_json,result_text,created_at) VALUES (?,?,?,?,?,?)''',
                (key,event_id,'__skill_assets__','{}',requested,datetime.now(timezone.utc).isoformat()))

    def freeze_tool_catalog(self, event_id, requested):
        """Existing unversioned executions remain legacy; the first compact request freezes its version."""
        if requested not in CATALOG_VERSIONS:
            raise ValueError('Unknown tool catalog version')
        key = 'catalog:' + hashlib.sha256(event_id.encode()).hexdigest()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?', (key,)).fetchone()
            if saved:
                version = json.loads(saved['result_text'])['catalog_version']
                if version not in CATALOG_VERSIONS:
                    raise ValueError('Unknown persisted tool catalog version')
                return version
            if requested == LEGACY_CATALOG:
                return LEGACY_CATALOG
            event = db.execute('SELECT status,attempt_count FROM processed_events WHERE event_id=?', (event_id,)).fetchone()
            prior = db.execute("SELECT 1 FROM event_tool_results WHERE event_id=? AND tool_name<>'__skill_assets__' LIMIT 1", (event_id,)).fetchone()
            if not event or event['status'] != 'processing' or event['attempt_count'] > 1 or prior:
                return LEGACY_CATALOG
            db.execute('''INSERT INTO event_tool_results
                (operation_key,event_id,tool_name,arguments_json,result_text,created_at) VALUES (?,?,?,?,?,?)''',
                (key,event_id,'__tool_catalog__','{}',json.dumps({'catalog_version':requested}),datetime.now(timezone.utc).isoformat()))
        return requested

    def begin_event(
            self,
            event_id: str,
            now: datetime | None = None,
            lease_duration: timedelta = EVENT_PROCESSING_LEASE,
            ) -> EventClaim | None:
        started_at = now or datetime.now(timezone.utc)
        lease_expires_at = started_at + lease_duration
        claim_token = uuid.uuid4().hex
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO processed_events (
                    event_id,
                    status,
                    created_at,
                    updated_at,
                    lease_expires_at,
                    claim_token,
                    attempt_count,
                    last_error
                ) VALUES (?, 'processing', ?, ?, ?, ?, 1, NULL)
                ON CONFLICT(event_id) DO UPDATE SET
                    status = 'processing',
                    updated_at = excluded.updated_at,
                    lease_expires_at = excluded.lease_expires_at,
                    claim_token = excluded.claim_token,
                    attempt_count = processed_events.attempt_count + 1,
                    last_error = NULL
                WHERE processed_events.status = 'failed'
                   OR (
                        processed_events.status = 'processing'
                        AND (
                            processed_events.lease_expires_at IS NULL
                            OR julianday(processed_events.lease_expires_at)
                                <= julianday(excluded.updated_at)
                        )
                   )
                """,
                (
                    event_id,
                    started_at.isoformat(),
                    started_at.isoformat(),
                    lease_expires_at.isoformat(),
                    claim_token,
                ),
            )
            row = connection.execute(
                """
                SELECT prepared_reply, prepared_memory_content
                FROM processed_events
                WHERE event_id = ? AND claim_token = ?
                """,
                (event_id, claim_token),
            ).fetchone()
        if cursor.rowcount != 1:
            return None
        return EventClaim(
            event_id=event_id,
            claim_token=claim_token,
            prepared_reply=row["prepared_reply"],
            prepared_memory_content=row["prepared_memory_content"],
        )


    def renew_event(
            self,
            event_id: str,
            claim_token: str,
            now: datetime | None = None,
            lease_duration: timedelta = EVENT_PROCESSING_LEASE) -> bool:
        renewed_at = now or datetime.now(timezone.utc)
        lease_expires_at = renewed_at + lease_duration
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processed_events
                SET updated_at = ?, lease_expires_at = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (
                    renewed_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1


    def get_event_tool_result(self, operation_key: str) -> str | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT result_text FROM event_tool_results "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return str(row["result_text"]) if row is not None else None


    def save_event_tool_result(
            self,
            operation_key: str,
            event_id: str,
            tool_name: str,
            arguments: dict[str, object],
            result_text: str,
            now: datetime | None = None) -> str:
        created_at = now or datetime.now(timezone.utc)
        arguments_json = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_tool_results (
                    operation_key, event_id, tool_name, arguments_json,
                    result_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_key,
                    event_id,
                    tool_name,
                    arguments_json,
                    result_text,
                    created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT result_text FROM event_tool_results "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return str(row["result_text"])


    def claim_event(
            self,
            event_id: str,
            now: datetime | None = None,
            lease_duration: timedelta = EVENT_PROCESSING_LEASE) -> bool:
        """Keep the legacy one-step, permanently deduplicated claim behavior."""
        claim = self.begin_event(event_id, now, lease_duration)
        if claim is None:
            return False
        return self.complete_event(
            claim.event_id,
            claim.claim_token,
            now=now,
        )


    def prepare_event_reply(
            self,
            event_id: str,
            claim_token: str,
            reply: str,
            memory_content: str | None = None,
            now: datetime | None = None) -> bool:
        prepared_at = now or datetime.now(timezone.utc)
        lease_expires_at = prepared_at + EVENT_PROCESSING_LEASE
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processed_events
                SET prepared_reply = ?,
                    prepared_memory_content = ?,
                    updated_at = ?,
                    lease_expires_at = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (
                    reply,
                    memory_content,
                    prepared_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1


    def prepare_event_outbox(
            self,
            event_id: str,
            claim_token: str,
            platform: str,
            channel: str,
            target_id: str,
            sender_id: str,
            reply_to_id: str,
            payload: dict[str, object],
            memory_content: str | None,
            now: datetime | None = None,
            *,
            assistant_text: str | None = None) -> int:
        prepared_at = now or datetime.now(timezone.utc)
        lease_expires_at = prepared_at + EVENT_PROCESSING_LEASE
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        prepared_reply = assistant_text if assistant_text is not None else self._payload_reply_text(payload)
        with self.store._connect() as connection:
            event = connection.execute(
                """
                SELECT status, claim_token
                FROM processed_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if (
                    event is None
                    or event["status"] != "processing"
                    or event["claim_token"] != claim_token):
                raise RuntimeError(f"Lost event processing lease: {event_id}")

            existing = connection.execute(
                "SELECT id FROM outbox_messages WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

            cursor = connection.execute(
                """
                UPDATE processed_events
                SET prepared_reply = ?,
                    prepared_memory_content = ?,
                    updated_at = ?,
                    lease_expires_at = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (
                    prepared_reply,
                    memory_content,
                    prepared_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Lost event processing lease: {event_id}")
            cursor = connection.execute(
                """
                INSERT INTO outbox_messages (
                    event_id,
                    platform,
                    channel,
                    target_id,
                    sender_id,
                    reply_to_id,
                    payload_json,
                    status,
                    attempt_count,
                    next_attempt_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event_id,
                    platform,
                    channel,
                    target_id,
                    sender_id,
                    reply_to_id,
                    payload_json,
                    prepared_at.isoformat(),
                    prepared_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid)


    @staticmethod
    def _payload_reply_text(payload: dict[str, object]) -> str:
        message = payload.get("message")
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            return "".join(
                segment["data"]["text"] for segment in message
                if isinstance(segment, dict) and segment.get("type") == "text"
                and isinstance(segment.get("data"), dict)
                and isinstance(segment["data"].get("text"), str)
            )
        content = payload.get("content")
        if isinstance(content, str):
            return content
        markdown = payload.get("markdown")
        if isinstance(markdown, dict):
            markdown_content = markdown.get("content")
            if isinstance(markdown_content, str):
                return markdown_content
        return ""


    def expire_outbox_if_needed(self, outbox_id, token, now):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT event_id, created_at FROM outbox_messages WHERE id=? AND status='sending' AND claim_token=?",
                                     (outbox_id, token)).fetchone()
            if not row:
                return False
            event_id = row['event_id']
            expired = False
            if event_id.startswith('reminder:'):
                occurrence = connection.execute('SELECT scheduled_at_utc FROM reminder_occurrences WHERE outbox_event_id=?', (event_id,)).fetchone()
                expired = bool(occurrence and datetime.fromisoformat(occurrence['scheduled_at_utc']) < now-timedelta(hours=24))
            elif event_id.startswith('scheduled-query:'):
                query = connection.execute('SELECT due_at FROM scheduled_queries WHERE output_event_id=?', (event_id,)).fetchone()
                expired = datetime.fromisoformat(query['due_at'] if query else row['created_at']) < now-timedelta(hours=24)
            elif event_id.startswith('reservation-reminder:'):
                item = connection.execute('''SELECT i.visit_date FROM reservation_reminders r JOIN reservation_items i
                    ON i.id=r.reservation_item_id WHERE r.outbox_event_id=?''', (event_id,)).fetchone()
                expired = bool(item and item['visit_date'] and date.fromisoformat(item['visit_date']) < now.astimezone(ZoneInfo('Asia/Shanghai')).date())
            elif event_id.startswith('policy-watch:'):
                source = connection.execute('SELECT created_at FROM processed_events WHERE event_id=?', (event_id,)).fetchone()
                expired = bool(source and datetime.fromisoformat(source['created_at']) < now-timedelta(hours=24))
            else:
                task = connection.execute('''SELECT t.task_type, p.created_at FROM task_event_results e
                    JOIN travel_tasks t ON t.task_id=e.task_id JOIN processed_events p ON p.event_id=e.event_id
                    WHERE e.event_id=?''', (event_id,)).fetchone()
                if task and task['task_type'] in {'weather', 'forecast', 'traffic', 'route', 'walking', 'transit'}:
                    expired = datetime.fromisoformat(task['created_at']) < now-timedelta(hours=24)
            if not expired:
                return False
            connection.execute("UPDATE outbox_messages SET status='cancelled', last_error='delivery_expired', claim_token=NULL, lease_expires_at=NULL WHERE id=?", (outbox_id,))
            connection.execute("UPDATE processed_events SET status='completed', prepared_reply=NULL, prepared_memory_content=NULL, claim_token=NULL, lease_expires_at=NULL WHERE event_id=?", (event_id,))
            connection.execute("UPDATE reminder_occurrences SET status='missed', last_error='delivery_expired' WHERE outbox_event_id=?", (event_id,))
            connection.execute("UPDATE scheduled_queries SET status='missed' WHERE output_event_id=?", (event_id,))
            connection.execute("UPDATE reservation_reminders SET status='expired', last_error='visit_date_passed' WHERE outbox_event_id=?", (event_id,))
        return True


    def list_due_outbox(
            self,
            platform: str,
            now: datetime | None = None,
            limit: int = 20) -> tuple[OutboxMessage, ...]:
        current = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM outbox_messages
                WHERE platform = ?
                  AND (
                    (
                        status IN ('pending', 'failed')
                        AND julianday(next_attempt_at) <= julianday(?)
                    )
                    OR (
                        status = 'sending'
                        AND (
                            lease_expires_at IS NULL
                            OR julianday(lease_expires_at) <= julianday(?)
                        )
                    )
                  )
                ORDER BY id
                LIMIT ?
                """,
                (
                    platform,
                    current.isoformat(),
                    current.isoformat(),
                    limit,
                ),
            ).fetchall()
        return tuple(self._outbox_message(row) for row in rows)


    def list_outbox_for_event(
            self,
            event_id: str) -> tuple[OutboxMessage, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox_messages WHERE event_id = ? ORDER BY id",
                (event_id,),
            ).fetchall()
        return tuple(self._outbox_message(row) for row in rows)


    @staticmethod
    def _outbox_message(row: sqlite3.Row) -> OutboxMessage:
        return OutboxMessage(
            outbox_id=int(row["id"]),
            event_id=str(row["event_id"]),
            platform=str(row["platform"]),
            channel=str(row["channel"]),
            target_id=str(row["target_id"]),
            sender_id=str(row["sender_id"]),
            reply_to_id=str(row["reply_to_id"] or ""),
            payload=json.loads(row["payload_json"]),
            attempt_count=int(row["attempt_count"]),
        )


    def claim_outbox(
            self,
            outbox_id: int,
            now: datetime | None = None,
            lease_duration: timedelta = timedelta(minutes=2),
            ) -> str | None:
        claimed_at = now or datetime.now(timezone.utc)
        claim_token = uuid.uuid4().hex
        lease_expires_at = claimed_at + lease_duration
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'sending',
                    attempt_count = attempt_count + 1,
                    lease_expires_at = ?,
                    claim_token = ?,
                    last_error = NULL
                WHERE id = ?
                  AND (
                    (
                        status IN ('pending', 'failed')
                        AND julianday(next_attempt_at) <= julianday(?)
                    )
                    OR (
                        status = 'sending'
                        AND (
                            lease_expires_at IS NULL
                            OR julianday(lease_expires_at) <= julianday(?)
                        )
                    )
                  )
                """,
                (
                    lease_expires_at.isoformat(),
                    claim_token,
                    outbox_id,
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                ),
            )
        return claim_token if cursor.rowcount == 1 else None


    def mark_outbox_failed(
            self,
            outbox_id: int,
            claim_token: str,
            error: str,
            next_attempt_at: datetime,
            max_attempts: int = 8,
            permanent: bool = False) -> bool:
        error_text = str(error)[:MAX_EVENT_ERROR_CHARS]
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    o.event_id,
                    o.attempt_count,
                    r.status AS reminder_status
                FROM outbox_messages o
                LEFT JOIN reservation_reminders r
                  ON r.outbox_event_id = o.event_id
                WHERE o.id = ?
                  AND o.status = 'sending'
                  AND o.claim_token = ?
                """,
                (outbox_id, claim_token),
            ).fetchone()
            if row is None:
                return False

            cancelled_delivery = row["reminder_status"] == "cancelled"
            terminal = not cancelled_delivery and (
                permanent or int(row["attempt_count"]) >= max_attempts
            )
            if cancelled_delivery:
                cursor = connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'cancelled',
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = ?
                    WHERE id = ?
                      AND status = 'sending'
                      AND claim_token = ?
                    """,
                    (error_text, outbox_id, claim_token),
                )
            elif terminal:
                cursor = connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'dead_letter',
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = ?
                    WHERE id = ?
                      AND status = 'sending'
                      AND claim_token = ?
                    """,
                    (error_text, outbox_id, claim_token),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'failed',
                        next_attempt_at = ?,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = ?
                    WHERE id = ?
                      AND status = 'sending'
                      AND claim_token = ?
                    """,
                    (
                        next_attempt_at.isoformat(),
                        error_text,
                        outbox_id,
                        claim_token,
                    ),
                )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE reservation_reminders
                SET status = CASE WHEN ? THEN 'failed' ELSE status END,
                    last_error = ?
                WHERE outbox_event_id = ?
                """,
                (terminal, error_text, row["event_id"]),
            )
            if terminal:
                connection.execute(
                    """
                    UPDATE processed_events
                    SET status = 'dead_letter',
                        updated_at = ?,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = ?,
                        prepared_reply = NULL,
                        prepared_memory_content = NULL
                    WHERE event_id = ?
                    """,
                    (
                        next_attempt_at.isoformat(),
                        error_text,
                        row["event_id"],
                    ),
                )
            elif cancelled_delivery:
                connection.execute(
                    """
                    UPDATE processed_events
                    SET status = 'completed',
                        updated_at = ?,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = NULL,
                        prepared_reply = NULL,
                        prepared_memory_content = NULL
                    WHERE event_id = ?
                    """,
                    (next_attempt_at.isoformat(), row["event_id"]),
                )
        return True


    def mark_outbox_sent(
            self,
            outbox_id: int,
            claim_token: str,
            now: datetime | None = None,
            *,
            platform_message_id: str | None = None) -> bool:
        sent_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    outbox_messages.*,
                    processed_events.prepared_reply,
                    processed_events.prepared_memory_content
                FROM outbox_messages
                JOIN processed_events
                  ON processed_events.event_id = outbox_messages.event_id
                WHERE outbox_messages.id = ?
                  AND outbox_messages.status = 'sending'
                  AND outbox_messages.claim_token = ?
                """,
                (outbox_id, claim_token),
            ).fetchone()
            if row is None:
                return False

            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'sent',
                    sent_at = ?,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    last_error = NULL
                WHERE id = ?
                  AND status = 'sending'
                  AND claim_token = ?
                """,
                (sent_at.isoformat(), outbox_id, claim_token),
            )
            if cursor.rowcount != 1:
                return False

            if platform_message_id:
                connection.execute("""
                    INSERT OR IGNORE INTO outbound_message_links
                    (platform, scope_id, message_id, event_id, owner_id) VALUES (?, ?, ?, ?, ?)
                """, (row['platform'], storage_scope_id(str(row['platform']), str(row['target_id'])),
                      platform_message_id, row['event_id'], row['sender_id']))

            connection.execute(
                """
                UPDATE reservation_reminders
                SET status = 'sent',
                    sent_at = ?,
                    last_error = NULL
                WHERE outbox_event_id = ?
                  AND status = 'queued'
                """,
                (sent_at.isoformat(), row["event_id"]),
            )

            if row["channel"] == "group" and not str(row['event_id']).startswith(('reminder:', 'inbox-notice:', 'scheduled-query:', 'policy-watch:')):
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
                        storage_scope_id(
                            str(row["platform"]),
                            str(row["target_id"]),
                        ),
                        row["sender_id"],
                        row["event_id"],
                        row["prepared_memory_content"] or "",
                        row["prepared_reply"] or "",
                        sent_at.isoformat(),
                    ),
                )

            connection.execute(
                """
                UPDATE processed_events
                SET status = 'completed',
                    updated_at = ?,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    last_error = NULL,
                    prepared_reply = NULL,
                    prepared_memory_content = NULL
                WHERE event_id = ?
                """,
                (sent_at.isoformat(), row["event_id"]),
            )
        return True


    def complete_event(
            self,
            event_id: str,
            claim_token: str,
            now: datetime | None = None) -> bool:
        completed_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processed_events
                SET status = 'completed',
                    updated_at = ?,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    last_error = NULL,
                    prepared_reply = NULL,
                    prepared_memory_content = NULL
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (completed_at.isoformat(), event_id, claim_token),
            )
        return cursor.rowcount == 1


    def fail_event(
            self,
            event_id: str,
            claim_token: str,
            error: str = "",
            now: datetime | None = None) -> bool:
        failed_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processed_events
                SET status = 'failed',
                    updated_at = ?,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    last_error = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (
                    failed_at.isoformat(),
                    str(error)[:MAX_EVENT_ERROR_CHARS],
                    event_id,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1


    def get_event_status(self, event_id: str) -> str | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT status FROM processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row["status"] if row else None


    def runtime_health(
            self,
            platform: str,
            now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            outbox_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM outbox_messages
                WHERE platform = ?
                GROUP BY status
                """,
                (platform,),
            ).fetchall()
            reminder_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM reservation_reminders
                WHERE platform = ?
                GROUP BY status
                """,
                (platform,),
            ).fetchall()
            stale_events = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM processed_events
                WHERE status = 'processing'
                  AND lease_expires_at IS NOT NULL
                  AND julianday(lease_expires_at) <= julianday(?)
                """,
                (current.isoformat(),),
            ).fetchone()
        outbox = {
            str(row["status"]): int(row["count"])
            for row in outbox_rows
        }
        reminders = {
            str(row["status"]): int(row["count"])
            for row in reminder_rows
        }
        return {
            "outbox": outbox,
            "reminders": reminders,
            "dead_letters": int(outbox.get("dead_letter", 0)),
            "stale_processing_events": int(stale_events["count"]),
        }

