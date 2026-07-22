from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from chat_transport import storage_scope_id

if TYPE_CHECKING:
    from document_service import PreparedDocument


RECENT_TURN_LIMIT = 6
MAX_DOCUMENT_CONTEXT_CHARS = 3200
MAX_DOCUMENT_CHUNKS = 2
MAX_DOCUMENT_CANDIDATES = 10
MAX_FTS_QUERY_TERMS = 24
MAX_FALLBACK_QUERY_TERMS = 12
DOCUMENT_FTS_MIGRATION = "document_chunks_fts_v1"
EVENT_PROCESSING_LEASE = timedelta(minutes=10)
MAX_EVENT_ERROR_CHARS = 1000


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


@dataclass(frozen=True)
class StoredDocument:
    document_id: int
    filename: str
    is_new: bool


@dataclass(frozen=True)
class UploadBinding:
    binding_id: int
    group_openid: str
    issuer_openid: str
    c2c_user_openid: str
    expires_at: str


@dataclass(frozen=True)
class UploadBindingRedemption:
    status: str
    binding: UploadBinding | None = None


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


@dataclass(frozen=True)
class ReservationImageRecord:
    image_id: int
    storage_scope_id: str
    platform: str
    group_id: str
    uploader_id: str
    sha256: str
    file_path: str
    content_type: str
    byte_size: int
    extracted_text: str
    extraction: dict[str, object]
    model_id: str
    status: str
    last_error: str


@dataclass(frozen=True)
class ReservationItemRecord:
    item_id: int
    public_code: str
    plan_id: int
    item_index: int
    attraction_name: str
    price_text: str
    opening_hours: str
    booking_channel: str
    source_text: str
    confidence: float
    requires_reservation: bool
    advance_value: int
    advance_unit: str
    visit_date: date | None
    booking_date: date | None
    date_candidates: tuple[date, ...]
    custom_reminder_times: tuple[datetime, ...]
    reminder_policy: str
    status: str


@dataclass(frozen=True)
class ReservationPlanRecord:
    plan_id: int
    plan_code: str
    image_id: int
    platform: str
    group_id: str
    creator_id: str
    status: str
    items: tuple[ReservationItemRecord, ...]


@dataclass(frozen=True)
class ReservationReminderRecord:
    reminder_id: int
    reservation_item_id: int
    platform: str
    group_id: str
    recipient_id: str
    scheduled_at_utc: datetime
    status: str
    outbox_event_id: str
    is_custom: bool
    last_error: str


@dataclass(frozen=True)
class ReservationMutationResult:
    item: ReservationItemRecord
    sending_warning: bool


@dataclass(frozen=True)
class DueReservationReminder:
    reminder_id: int
    reservation_item_id: int
    platform: str
    group_id: str
    recipient_id: str
    scheduled_at_utc: datetime
    attraction_name: str
    visit_date: date
    booking_date: date
    opening_hours: str
    price_text: str
    booking_channel: str


class MemoryStore:
    def __init__(self, database_path: str | Path | None = None):
        default_path = Path(__file__).resolve().parent / "data" / "travel_bot.db"
        self.database_path = Path(database_path or default_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._document_fts_available = False
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    member_openid TEXT NOT NULL,
                    user_msg_id TEXT NOT NULL UNIQUE,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session
                ON conversation_turns(group_openid, member_openid, id DESC);

                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    reply_to_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chat_messages_group_time
                ON chat_messages(platform, group_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    uploader_openid TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    text_length INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_openid, sha256)
                );

                CREATE INDEX IF NOT EXISTS idx_documents_group
                ON documents(group_openid, id DESC);

                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                        ON DELETE CASCADE,
                    UNIQUE(document_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS upload_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT NOT NULL UNIQUE,
                    group_openid TEXT NOT NULL,
                    issuer_openid TEXT NOT NULL,
                    c2c_user_openid TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    redeemed_at TEXT,
                    consumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_upload_bindings_private_user
                ON upload_bindings(c2c_user_openid, id DESC);

                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    claim_token TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT,
                    prepared_reply TEXT,
                    prepared_memory_content TEXT
                );

                CREATE TABLE IF NOT EXISTS outbox_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    reply_to_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    claim_token TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    FOREIGN KEY(event_id) REFERENCES processed_events(event_id)
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON outbox_messages(status, next_attempt_at, lease_expires_at);

                CREATE TABLE IF NOT EXISTS reservation_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    storage_scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    uploader_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    extraction_json TEXT NOT NULL DEFAULT '{}',
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(storage_scope_id, sha256)
                );

                CREATE TABLE IF NOT EXISTS reservation_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_code TEXT NOT NULL UNIQUE,
                    image_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    cancelled_at TEXT,
                    FOREIGN KEY(image_id) REFERENCES reservation_images(id)
                );

                CREATE TABLE IF NOT EXISTS reservation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_code TEXT UNIQUE,
                    plan_id INTEGER NOT NULL,
                    item_index INTEGER NOT NULL,
                    attraction_name TEXT NOT NULL,
                    price_text TEXT NOT NULL DEFAULT '',
                    opening_hours TEXT NOT NULL DEFAULT '',
                    booking_channel TEXT NOT NULL DEFAULT '',
                    source_text TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    requires_reservation INTEGER NOT NULL,
                    advance_value INTEGER NOT NULL,
                    advance_unit TEXT NOT NULL,
                    visit_date TEXT,
                    booking_date TEXT,
                    date_candidates_json TEXT NOT NULL DEFAULT '[]',
                    custom_reminder_times_json TEXT NOT NULL DEFAULT '[]',
                    reminder_policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES reservation_plans(id)
                        ON DELETE CASCADE,
                    UNIQUE(plan_id, item_index)
                );

                CREATE TABLE IF NOT EXISTS reservation_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_item_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outbox_event_id TEXT UNIQUE,
                    is_custom INTEGER NOT NULL,
                    queued_at TEXT,
                    sent_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(reservation_item_id)
                        REFERENCES reservation_items(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_reservation_reminders_due
                ON reservation_reminders(
                    platform, status, scheduled_at_utc
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_reservation_reminder_identity
                ON reservation_reminders(
                    reservation_item_id,
                    scheduled_at_utc,
                    is_custom
                )
                WHERE status IN ('pending', 'queued');

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }
            if "summary" not in columns:
                connection.execute(
                    "ALTER TABLE documents "
                    "ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )

            event_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(processed_events)"
                ).fetchall()
            }
            if "status" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            if "updated_at" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events "
                    "ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "UPDATE processed_events SET updated_at = created_at "
                    "WHERE updated_at = ''"
                )
            if "lease_expires_at" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events "
                    "ADD COLUMN lease_expires_at TEXT"
                )
            if "claim_token" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events ADD COLUMN claim_token TEXT"
                )
            if "attempt_count" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events "
                    "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
                )
            if "last_error" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events ADD COLUMN last_error TEXT"
                )
            if "prepared_reply" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events ADD COLUMN prepared_reply TEXT"
                )
            if "prepared_memory_content" not in event_columns:
                connection.execute(
                    "ALTER TABLE processed_events "
                    "ADD COLUMN prepared_memory_content TEXT"
                )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_events_status_lease
                ON processed_events(status, lease_expires_at)
                """
            )
            self._initialize_document_search(connection)

    def _initialize_document_search(
            self,
            connection: sqlite3.Connection) -> None:
        fts_existed = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'document_chunks_fts'"
        ).fetchone() is not None
        migration_applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (DOCUMENT_FTS_MIGRATION,),
        ).fetchone() is not None

        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
                USING fts5(
                    content,
                    content='document_chunks',
                    content_rowid='id',
                    tokenize='trigram'
                )
                """
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_insert
                AFTER INSERT ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_delete
                AFTER DELETE ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(
                        document_chunks_fts, rowid, content
                    ) VALUES ('delete', old.id, old.content);
                END;

                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_update
                AFTER UPDATE OF content ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(
                        document_chunks_fts, rowid, content
                    ) VALUES ('delete', old.id, old.content);
                    INSERT INTO document_chunks_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                """
            )
            if not fts_existed or not migration_applied:
                connection.execute(
                    "INSERT INTO document_chunks_fts(document_chunks_fts) "
                    "VALUES ('rebuild')"
                )
                connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations (name, applied_at) "
                    "VALUES (?, ?)",
                    (
                        DOCUMENT_FTS_MIGRATION,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self._document_fts_available = True
        except sqlite3.OperationalError:
            # Some custom SQLite builds omit FTS5 or the trigram tokenizer.
            # The query path below falls back to bounded SQL substring search.
            self._document_fts_available = False

    def begin_event(
            self,
            event_id: str,
            now: datetime | None = None,
            lease_duration: timedelta = EVENT_PROCESSING_LEASE,
            ) -> EventClaim | None:
        started_at = now or datetime.now(timezone.utc)
        lease_expires_at = started_at + lease_duration
        claim_token = uuid.uuid4().hex
        with self._connect() as connection:
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
        with self._connect() as connection:
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
            now: datetime | None = None) -> int:
        prepared_at = now or datetime.now(timezone.utc)
        lease_expires_at = prepared_at + EVENT_PROCESSING_LEASE
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        prepared_reply = self._payload_reply_text(payload)
        with self._connect() as connection:
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
        content = payload.get("content")
        if isinstance(content, str):
            return content
        markdown = payload.get("markdown")
        if isinstance(markdown, dict):
            markdown_content = markdown.get("content")
            if isinstance(markdown_content, str):
                return markdown_content
        return ""

    def list_due_outbox(
            self,
            platform: str,
            now: datetime | None = None,
            limit: int = 20) -> tuple[OutboxMessage, ...]:
        current = now or datetime.now(timezone.utc)
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
            next_attempt_at: datetime) -> bool:
        error_text = str(error)[:MAX_EVENT_ERROR_CHARS]
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    o.event_id,
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
            if row["reminder_status"] == "cancelled":
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
                SET last_error = ?
                WHERE outbox_event_id = ?
                """,
                (error_text, row["event_id"]),
            )
        return True

    def mark_outbox_sent(
            self,
            outbox_id: int,
            claim_token: str,
            now: datetime | None = None) -> bool:
        sent_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
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

            if row["channel"] == "group":
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row["status"] if row else None

    def create_reservation_image(
            self,
            storage_scope_id: str,
            platform: str,
            group_id: str,
            uploader_id: str,
            sha256: str,
            file_path: str,
            content_type: str,
            byte_size: int,
            model_id: str,
            now: datetime | None = None,
            ) -> tuple[ReservationImageRecord, bool]:
        created_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO reservation_images (
                    storage_scope_id, platform, group_id, uploader_id,
                    sha256, file_path, content_type, byte_size, model_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    storage_scope_id,
                    platform,
                    group_id,
                    uploader_id,
                    sha256,
                    file_path,
                    content_type,
                    byte_size,
                    model_id,
                    created_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            is_new = cursor.rowcount == 1
            row = connection.execute(
                """
                SELECT * FROM reservation_images
                WHERE storage_scope_id = ? AND sha256 = ?
                """,
                (storage_scope_id, sha256),
            ).fetchone()
        return self._reservation_image(row), is_new

    def get_reservation_image(
            self,
            storage_scope_id: str,
            sha256: str) -> ReservationImageRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM reservation_images
                WHERE storage_scope_id = ? AND sha256 = ?
                """,
                (storage_scope_id, sha256),
            ).fetchone()
        return self._reservation_image(row) if row is not None else None

    def mark_reservation_image_extracted(
            self,
            image_id: int,
            extracted_text: str,
            extraction: dict[str, object],
            model_id: str,
            now: datetime | None = None) -> bool:
        updated_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_images
                SET extracted_text = ?,
                    extraction_json = ?,
                    model_id = ?,
                    status = 'extracted',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    extracted_text,
                    json.dumps(
                        extraction,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    model_id,
                    updated_at.isoformat(),
                    image_id,
                ),
            )
        return cursor.rowcount == 1

    def mark_reservation_image_failed(
            self,
            image_id: int,
            error: str,
            now: datetime | None = None) -> bool:
        updated_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_images
                SET status = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    str(error)[:MAX_EVENT_ERROR_CHARS],
                    updated_at.isoformat(),
                    image_id,
                ),
            )
        return cursor.rowcount == 1

    def restart_failed_reservation_image(
            self,
            image_id: int,
            now: datetime | None = None) -> bool:
        updated_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_images
                SET status = 'pending',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (updated_at.isoformat(), image_id),
            )
        return cursor.rowcount == 1

    def create_reservation_draft(
            self,
            image_id: int,
            platform: str,
            group_id: str,
            creator_id: str,
            items: tuple[dict[str, object], ...],
            now: datetime | None = None) -> ReservationPlanRecord:
        created_at = now or datetime.now(timezone.utc)
        local_day = created_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y%m%d")
        prefix = f"R-{local_day}-"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT MAX(
                    CAST(substr(plan_code, 12) AS INTEGER)
                ) AS sequence
                FROM reservation_plans
                WHERE plan_code LIKE ?
                """,
                (f"{prefix}%",),
            ).fetchone()
            sequence = int(row["sequence"] or 0) + 1
            plan_code = f"{prefix}{sequence:03d}"
            cursor = connection.execute(
                """
                INSERT INTO reservation_plans (
                    plan_code, image_id, platform, group_id, creator_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    plan_code,
                    image_id,
                    platform,
                    group_id,
                    creator_id,
                    created_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            plan_id = int(cursor.lastrowid)
            for item_index, item in enumerate(items, start=1):
                extraction = item["extraction"]
                visit_date = item["visit_date"]
                booking_date = item["booking_date"]
                item_cursor = connection.execute(
                    """
                    INSERT INTO reservation_items (
                        plan_id, item_index, attraction_name, price_text,
                        opening_hours, booking_channel, source_text,
                        confidence, requires_reservation, advance_value,
                        advance_unit, visit_date, booking_date,
                        date_candidates_json,
                        custom_reminder_times_json,
                        reminder_policy, status, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        plan_id,
                        item_index,
                        extraction.attraction_name,
                        extraction.price_text,
                        extraction.opening_hours,
                        extraction.booking_channel,
                        extraction.source_text,
                        extraction.confidence,
                        int(extraction.requires_reservation),
                        extraction.advance_value,
                        extraction.advance_unit,
                        visit_date.isoformat() if visit_date else None,
                        booking_date.isoformat() if booking_date else None,
                        json.dumps([
                            value.isoformat()
                            for value in item["date_candidates"]
                        ]),
                        json.dumps([
                            value.astimezone(
                                ZoneInfo("Asia/Shanghai")
                            ).isoformat()
                            for value in item["custom_reminder_times"]
                        ]),
                        item["reminder_policy"],
                        item["status"],
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
                item_id = int(item_cursor.lastrowid)
                connection.execute(
                    """
                    UPDATE reservation_items
                    SET public_code = ? WHERE id = ?
                    """,
                    (f"A-{item_id:06d}", item_id),
                )
        loaded = self.get_reservation_plan(
            platform,
            group_id,
            plan_code,
        )
        if loaded is None:
            raise RuntimeError("reservation draft was not persisted")
        return loaded

    def get_reservation_plan(
            self,
            platform: str,
            group_id: str,
            plan_code: str) -> ReservationPlanRecord | None:
        with self._connect() as connection:
            plan = connection.execute(
                """
                SELECT * FROM reservation_plans
                WHERE platform = ? AND group_id = ? AND plan_code = ?
                """,
                (platform, group_id, plan_code),
            ).fetchone()
            if plan is None:
                return None
            item_rows = connection.execute(
                """
                SELECT * FROM reservation_items
                WHERE plan_id = ?
                ORDER BY item_index
                """,
                (plan["id"],),
            ).fetchall()
        return ReservationPlanRecord(
            plan_id=int(plan["id"]),
            plan_code=str(plan["plan_code"]),
            image_id=int(plan["image_id"]),
            platform=str(plan["platform"]),
            group_id=str(plan["group_id"]),
            creator_id=str(plan["creator_id"]),
            status=str(plan["status"]),
            items=tuple(
                self._reservation_item(row)
                for row in item_rows
            ),
        )

    def update_reservation_draft_item_date(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            visit_date: date,
            booking_date: date,
            now: datetime | None = None) -> bool:
        updated_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_items
                SET visit_date = ?,
                    booking_date = ?,
                    date_candidates_json = ?,
                    status = 'ready',
                    updated_at = ?
                WHERE id = (
                    SELECT reservation_items.id
                    FROM reservation_items
                    JOIN reservation_plans
                      ON reservation_plans.id = reservation_items.plan_id
                    WHERE reservation_plans.platform = ?
                      AND reservation_plans.group_id = ?
                      AND reservation_plans.creator_id = ?
                      AND reservation_plans.plan_code = ?
                      AND reservation_plans.status = 'draft'
                      AND reservation_items.item_index = ?
                      AND reservation_items.requires_reservation = 1
                )
                """,
                (
                    visit_date.isoformat(),
                    booking_date.isoformat(),
                    json.dumps([visit_date.isoformat()]),
                    updated_at.isoformat(),
                    platform,
                    group_id,
                    creator_id,
                    plan_code,
                    item_index,
                ),
            )
        return cursor.rowcount == 1

    def append_reservation_draft_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            extraction: object,
            visit_date: date | None,
            booking_date: date | None,
            reminder_policy: str,
            status: str,
            now: datetime | None = None) -> bool:
        created_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT id FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND plan_code = ?
                  AND status = 'draft'
                """,
                (platform, group_id, creator_id, plan_code),
            ).fetchone()
            if plan is None:
                return False
            index_row = connection.execute(
                """
                SELECT COALESCE(MAX(item_index), 0) + 1 AS next_index
                FROM reservation_items WHERE plan_id = ?
                """,
                (plan["id"],),
            ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO reservation_items (
                    plan_id, item_index, attraction_name, price_text,
                    opening_hours, booking_channel, source_text, confidence,
                    requires_reservation, advance_value, advance_unit,
                    visit_date, booking_date, date_candidates_json,
                    custom_reminder_times_json, reminder_policy, status,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, '', '', '', ?, 1.0, ?, ?, ?, ?, ?, ?, '[]',
                    ?, ?, ?, ?
                )
                """,
                (
                    plan["id"],
                    index_row["next_index"],
                    extraction.attraction_name,
                    extraction.source_text,
                    int(extraction.requires_reservation),
                    extraction.advance_value,
                    extraction.advance_unit,
                    visit_date.isoformat() if visit_date else None,
                    booking_date.isoformat() if booking_date else None,
                    json.dumps(
                        [visit_date.isoformat()] if visit_date else []
                    ),
                    reminder_policy,
                    status,
                    created_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            item_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE reservation_items SET public_code = ? WHERE id = ?",
                (f"A-{item_id:06d}", item_id),
            )
        return True

    def set_reservation_draft_item_times(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            custom_times: tuple[datetime, ...],
            now: datetime | None = None) -> bool:
        updated_at = now or datetime.now(timezone.utc)
        serialized = json.dumps([
            value.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            for value in sorted(set(custom_times))
        ])
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_items
                SET custom_reminder_times_json = ?,
                    reminder_policy = 'custom',
                    updated_at = ?
                WHERE id = (
                    SELECT reservation_items.id
                    FROM reservation_items
                    JOIN reservation_plans
                      ON reservation_plans.id = reservation_items.plan_id
                    WHERE reservation_plans.platform = ?
                      AND reservation_plans.group_id = ?
                      AND reservation_plans.creator_id = ?
                      AND reservation_plans.plan_code = ?
                      AND reservation_plans.status = 'draft'
                      AND reservation_items.item_index = ?
                      AND reservation_items.requires_reservation = 1
                )
                """,
                (
                    serialized,
                    updated_at.isoformat(),
                    platform,
                    group_id,
                    creator_id,
                    plan_code,
                    item_index,
                ),
            )
        return cursor.rowcount == 1

    def confirm_reservation_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            reminders_by_item: dict[int, tuple[object, ...]],
            now: datetime | None = None) -> ReservationPlanRecord:
        confirmed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT * FROM reservation_plans
                WHERE platform = ? AND group_id = ? AND plan_code = ?
                """,
                (platform, group_id, plan_code),
            ).fetchone()
            if plan is None:
                raise ValueError("预约计划不存在")
            if plan["creator_id"] != creator_id:
                raise PermissionError("只有创建者可以确认预约计划")
            if plan["status"] == "confirmed":
                existing_code = str(plan["plan_code"])
            elif plan["status"] != "draft":
                raise ValueError("预约计划当前不能确认")
            else:
                incomplete = connection.execute(
                    """
                    SELECT 1 FROM reservation_items
                    WHERE plan_id = ?
                      AND requires_reservation = 1
                      AND status = 'needs_input'
                    LIMIT 1
                    """,
                    (plan["id"],),
                ).fetchone()
                if incomplete is not None:
                    raise ValueError("请先补充所有需要预约项目的日期")
                connection.execute(
                    """
                    UPDATE reservation_plans
                    SET status = 'confirmed',
                        confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        confirmed_at.isoformat(),
                        confirmed_at.isoformat(),
                        plan["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE reservation_items
                    SET status = 'confirmed', updated_at = ?
                    WHERE plan_id = ? AND status = 'ready'
                    """,
                    (confirmed_at.isoformat(), plan["id"]),
                )
                for item_id, occurrences in reminders_by_item.items():
                    for occurrence in occurrences:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO reservation_reminders (
                                reservation_item_id, platform, group_id,
                                recipient_id, scheduled_at_utc, status,
                                is_custom, created_at
                            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                            """,
                            (
                                item_id,
                                platform,
                                group_id,
                                creator_id,
                                occurrence.scheduled_at_utc.isoformat(),
                                int(occurrence.is_custom),
                                confirmed_at.isoformat(),
                            ),
                        )
                existing_code = str(plan["plan_code"])
        loaded = self.get_reservation_plan(
            platform,
            group_id,
            existing_code,
        )
        if loaded is None:
            raise RuntimeError(
                "confirmed reservation plan could not be loaded"
            )
        return loaded

    def list_reservation_plans_for_creator(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> tuple[ReservationPlanRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT plan_code FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                ORDER BY id DESC
                """,
                (platform, group_id, creator_id),
            ).fetchall()
        plans = []
        for row in rows:
            plan = self.get_reservation_plan(
                platform,
                group_id,
                str(row["plan_code"]),
            )
            if plan is not None:
                plans.append(plan)
        return tuple(plans)

    def group_has_reservation_plans(
            self,
            platform: str,
            group_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM reservation_plans
                WHERE platform = ? AND group_id = ?
                LIMIT 1
                """,
                (platform, group_id),
            ).fetchone()
        return row is not None

    def get_reservation_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str) -> ReservationItemRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT i.*
                FROM reservation_items i
                JOIN reservation_plans p ON p.id = i.plan_id
                WHERE p.platform = ?
                  AND p.group_id = ?
                  AND p.creator_id = ?
                  AND p.status = 'confirmed'
                  AND i.public_code = ?
                  AND i.status = 'confirmed'
                """,
                (platform, group_id, creator_id, public_code),
            ).fetchone()
        return self._reservation_item(row) if row is not None else None

    def list_reservation_reminders(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            include_cancelled: bool = False,
            ) -> tuple[ReservationReminderRecord, ...]:
        status_sql = (
            ""
            if include_cancelled
            else "AND r.status IN ('pending', 'queued', 'sent')"
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*
                FROM reservation_reminders r
                JOIN reservation_items i
                  ON i.id = r.reservation_item_id
                JOIN reservation_plans p
                  ON p.id = i.plan_id
                WHERE p.platform = ?
                  AND p.group_id = ?
                  AND p.creator_id = ?
                  {status_sql}
                ORDER BY r.scheduled_at_utc, r.id
                """,
                (platform, group_id, creator_id),
            ).fetchall()
        return tuple(self._reservation_reminder(row) for row in rows)

    def list_all_reservation_reminders(
            self) -> tuple[ReservationReminderRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reservation_reminders ORDER BY id"
            ).fetchall()
        return tuple(self._reservation_reminder(row) for row in rows)

    def list_due_reservation_reminders(
            self,
            platform: str,
            now: datetime | None = None,
            limit: int = 50) -> tuple[DueReservationReminder, ...]:
        current = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    r.id AS reminder_id,
                    r.reservation_item_id,
                    r.platform,
                    r.group_id,
                    r.recipient_id,
                    r.scheduled_at_utc,
                    i.attraction_name,
                    i.visit_date,
                    i.booking_date,
                    i.opening_hours,
                    i.price_text,
                    i.booking_channel
                FROM reservation_reminders r
                JOIN reservation_items i
                  ON i.id = r.reservation_item_id
                JOIN reservation_plans p
                  ON p.id = i.plan_id
                WHERE r.platform = ?
                  AND r.status = 'pending'
                  AND julianday(r.scheduled_at_utc) <= julianday(?)
                  AND i.status = 'confirmed'
                  AND p.status = 'confirmed'
                ORDER BY r.scheduled_at_utc, r.id
                LIMIT ?
                """,
                (platform, current.isoformat(), limit),
            ).fetchall()
        return tuple(
            DueReservationReminder(
                reminder_id=int(row["reminder_id"]),
                reservation_item_id=int(row["reservation_item_id"]),
                platform=str(row["platform"]),
                group_id=str(row["group_id"]),
                recipient_id=str(row["recipient_id"]),
                scheduled_at_utc=datetime.fromisoformat(
                    row["scheduled_at_utc"]
                ).astimezone(timezone.utc),
                attraction_name=str(row["attraction_name"]),
                visit_date=date.fromisoformat(row["visit_date"]),
                booking_date=date.fromisoformat(row["booking_date"]),
                opening_hours=str(row["opening_hours"]),
                price_text=str(row["price_text"]),
                booking_channel=str(row["booking_channel"]),
            )
            for row in rows
        )

    def mark_reservation_reminder_terminal(
            self,
            reminder_id: int,
            status: str,
            error: str = "",
            now: datetime | None = None) -> bool:
        if status not in {"expired", "blocked"}:
            raise ValueError(
                "terminal reminder status must be expired or blocked"
            )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservation_reminders
                SET status = ?, last_error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    status,
                    str(error)[:MAX_EVENT_ERROR_CHARS],
                    reminder_id,
                ),
            )
        return cursor.rowcount == 1

    def queue_reservation_reminder(
            self,
            reminder_id: int,
            payload: dict[str, object],
            prepared_reply: str,
            now: datetime | None = None) -> int | None:
        queued_at = now or datetime.now(timezone.utc)
        event_id = f"reservation-reminder:{reminder_id}"
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reminder = connection.execute(
                "SELECT * FROM reservation_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if reminder is None:
                return None
            existing = connection.execute(
                "SELECT id FROM outbox_messages WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            if (
                    reminder["status"] != "pending"
                    or datetime.fromisoformat(
                        reminder["scheduled_at_utc"]
                    ) > queued_at):
                return None

            connection.execute(
                """
                INSERT OR IGNORE INTO processed_events (
                    event_id, status, created_at, updated_at,
                    attempt_count, prepared_reply,
                    prepared_memory_content
                ) VALUES (?, 'processing', ?, ?, 1, ?, ?)
                """,
                (
                    event_id,
                    queued_at.isoformat(),
                    queued_at.isoformat(),
                    prepared_reply,
                    "自动预约提醒",
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO outbox_messages (
                    event_id, platform, channel, target_id, sender_id,
                    reply_to_id, payload_json, status, attempt_count,
                    next_attempt_at, created_at
                ) VALUES (
                    ?, ?, 'group', ?, ?, '', ?, 'pending', 0, ?, ?
                )
                """,
                (
                    event_id,
                    reminder["platform"],
                    reminder["group_id"],
                    reminder["recipient_id"],
                    payload_json,
                    queued_at.isoformat(),
                    queued_at.isoformat(),
                ),
            )
            outbox_id = int(cursor.lastrowid)
            updated = connection.execute(
                """
                UPDATE reservation_reminders
                SET status = 'queued',
                    outbox_event_id = ?,
                    queued_at = ?,
                    last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (event_id, queued_at.isoformat(), reminder_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("reservation reminder queue race")
        return outbox_id

    def _cancel_item_delivery_rows(
            self,
            connection: sqlite3.Connection,
            item_id: int) -> bool:
        sending = connection.execute(
            """
            SELECT 1
            FROM reservation_reminders
            JOIN outbox_messages
              ON outbox_messages.event_id =
                 reservation_reminders.outbox_event_id
            WHERE reservation_reminders.reservation_item_id = ?
              AND reservation_reminders.status IN ('pending', 'queued')
              AND outbox_messages.status = 'sending'
            LIMIT 1
            """,
            (item_id,),
        ).fetchone() is not None
        connection.execute(
            """
            UPDATE outbox_messages
            SET status = 'cancelled',
                lease_expires_at = NULL,
                claim_token = NULL
            WHERE event_id IN (
                SELECT outbox_event_id
                FROM reservation_reminders
                WHERE reservation_item_id = ?
            )
              AND status IN ('pending', 'failed')
            """,
            (item_id,),
        )
        connection.execute(
            """
            UPDATE reservation_reminders
            SET status = 'cancelled',
                last_error = NULL
            WHERE reservation_item_id = ?
              AND status IN ('pending', 'queued')
            """,
            (item_id,),
        )
        return sending

    def replace_reservation_item_schedule(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str,
            visit_date: date,
            booking_date: date,
            custom_times: tuple[datetime, ...],
            reminder_policy: str,
            occurrences: tuple[object, ...],
            now: datetime | None = None,
            ) -> ReservationMutationResult | None:
        changed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.id
                FROM reservation_items i
                JOIN reservation_plans p ON p.id = i.plan_id
                WHERE p.platform = ?
                  AND p.group_id = ?
                  AND p.creator_id = ?
                  AND p.status = 'confirmed'
                  AND i.public_code = ?
                  AND i.status = 'confirmed'
                """,
                (platform, group_id, creator_id, public_code),
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["id"])
            sending_warning = self._cancel_item_delivery_rows(
                connection,
                item_id,
            )
            connection.execute(
                """
                UPDATE reservation_items
                SET visit_date = ?,
                    booking_date = ?,
                    date_candidates_json = ?,
                    custom_reminder_times_json = ?,
                    reminder_policy = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    visit_date.isoformat(),
                    booking_date.isoformat(),
                    json.dumps([visit_date.isoformat()]),
                    json.dumps([
                        value.astimezone(
                            ZoneInfo("Asia/Shanghai")
                        ).isoformat()
                        for value in sorted(set(custom_times))
                    ]),
                    reminder_policy,
                    changed_at.isoformat(),
                    item_id,
                ),
            )
            for occurrence in occurrences:
                connection.execute(
                    """
                    INSERT INTO reservation_reminders (
                        reservation_item_id, platform, group_id,
                        recipient_id, scheduled_at_utc, status,
                        is_custom, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        item_id,
                        platform,
                        group_id,
                        creator_id,
                        occurrence.scheduled_at_utc.isoformat(),
                        int(occurrence.is_custom),
                        changed_at.isoformat(),
                    ),
                )
        item = self.get_reservation_item(
            platform,
            group_id,
            creator_id,
            public_code,
        )
        if item is None:
            raise RuntimeError(
                "updated reservation item could not be loaded"
            )
        return ReservationMutationResult(item, sending_warning)

    def cancel_reservation_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str,
            now: datetime | None = None,
            ) -> ReservationMutationResult | None:
        cancelled_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*
                FROM reservation_items i
                JOIN reservation_plans p ON p.id = i.plan_id
                WHERE p.platform = ?
                  AND p.group_id = ?
                  AND p.creator_id = ?
                  AND p.status = 'confirmed'
                  AND i.public_code = ?
                  AND i.status = 'confirmed'
                """,
                (platform, group_id, creator_id, public_code),
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["id"])
            sending_warning = self._cancel_item_delivery_rows(
                connection,
                item_id,
            )
            connection.execute(
                """
                UPDATE reservation_items
                SET status = 'cancelled', updated_at = ?
                WHERE id = ?
                """,
                (cancelled_at.isoformat(), item_id),
            )
            cancelled_row = connection.execute(
                "SELECT * FROM reservation_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        return ReservationMutationResult(
            self._reservation_item(cancelled_row),
            sending_warning,
        )

    def cancel_reservation_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            now: datetime | None = None) -> bool | None:
        cancelled_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT id FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND plan_code = ?
                  AND status IN ('draft', 'confirmed')
                """,
                (platform, group_id, creator_id, plan_code),
            ).fetchone()
            if plan is None:
                return None
            item_rows = connection.execute(
                "SELECT id FROM reservation_items WHERE plan_id = ?",
                (plan["id"],),
            ).fetchall()
            sending_warning = False
            for item_row in item_rows:
                sending_warning = (
                    self._cancel_item_delivery_rows(
                        connection,
                        int(item_row["id"]),
                    )
                    or sending_warning
                )
            connection.execute(
                """
                UPDATE reservation_items
                SET status = 'cancelled', updated_at = ?
                WHERE plan_id = ? AND status != 'cancelled'
                """,
                (cancelled_at.isoformat(), plan["id"]),
            )
            connection.execute(
                """
                UPDATE reservation_plans
                SET status = 'cancelled',
                    cancelled_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    cancelled_at.isoformat(),
                    cancelled_at.isoformat(),
                    plan["id"],
                ),
            )
        return sending_warning

    @staticmethod
    def _reservation_image(row: sqlite3.Row) -> ReservationImageRecord:
        extraction = json.loads(row["extraction_json"] or "{}")
        if not isinstance(extraction, dict):
            extraction = {}
        return ReservationImageRecord(
            image_id=int(row["id"]),
            storage_scope_id=str(row["storage_scope_id"]),
            platform=str(row["platform"]),
            group_id=str(row["group_id"]),
            uploader_id=str(row["uploader_id"]),
            sha256=str(row["sha256"]),
            file_path=str(row["file_path"]),
            content_type=str(row["content_type"]),
            byte_size=int(row["byte_size"]),
            extracted_text=str(row["extracted_text"] or ""),
            extraction=extraction,
            model_id=str(row["model_id"]),
            status=str(row["status"]),
            last_error=str(row["last_error"] or ""),
        )

    @staticmethod
    def _reservation_item(row: sqlite3.Row) -> ReservationItemRecord:
        visit_date = (
            date.fromisoformat(row["visit_date"])
            if row["visit_date"]
            else None
        )
        booking_date = (
            date.fromisoformat(row["booking_date"])
            if row["booking_date"]
            else None
        )
        date_candidates = tuple(
            date.fromisoformat(value)
            for value in json.loads(
                row["date_candidates_json"] or "[]"
            )
        )
        custom_reminder_times = tuple(
            datetime.fromisoformat(value).astimezone(timezone.utc)
            for value in json.loads(
                row["custom_reminder_times_json"] or "[]"
            )
        )
        return ReservationItemRecord(
            item_id=int(row["id"]),
            public_code=str(row["public_code"]),
            plan_id=int(row["plan_id"]),
            item_index=int(row["item_index"]),
            attraction_name=str(row["attraction_name"]),
            price_text=str(row["price_text"]),
            opening_hours=str(row["opening_hours"]),
            booking_channel=str(row["booking_channel"]),
            source_text=str(row["source_text"]),
            confidence=float(row["confidence"]),
            requires_reservation=bool(row["requires_reservation"]),
            advance_value=int(row["advance_value"]),
            advance_unit=str(row["advance_unit"]),
            visit_date=visit_date,
            booking_date=booking_date,
            date_candidates=date_candidates,
            custom_reminder_times=custom_reminder_times,
            reminder_policy=str(row["reminder_policy"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _reservation_reminder(
            row: sqlite3.Row) -> ReservationReminderRecord:
        return ReservationReminderRecord(
            reminder_id=int(row["id"]),
            reservation_item_id=int(row["reservation_item_id"]),
            platform=str(row["platform"]),
            group_id=str(row["group_id"]),
            recipient_id=str(row["recipient_id"]),
            scheduled_at_utc=datetime.fromisoformat(
                row["scheduled_at_utc"]
            ).astimezone(timezone.utc),
            status=str(row["status"]),
            outbox_event_id=str(row["outbox_event_id"] or ""),
            is_custom=bool(row["is_custom"]),
            last_error=str(row["last_error"] or ""),
        )

    def create_upload_binding(
            self,
            code_hash: str,
            group_openid: str,
            issuer_openid: str,
            expires_at: datetime,
            now: datetime | None = None) -> int:
        created_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO upload_bindings (
                    code_hash,
                    group_openid,
                    issuer_openid,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    code_hash,
                    group_openid,
                    issuer_openid,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def redeem_upload_binding(
            self,
            code_hash: str,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBindingRedemption:
        redeemed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_bindings
                SET c2c_user_openid = ?, redeemed_at = ?
                WHERE code_hash = ?
                  AND redeemed_at IS NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (
                    c2c_user_openid,
                    redeemed_at.isoformat(),
                    code_hash,
                    redeemed_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM upload_bindings WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if cursor.rowcount != 1:
                if row is None:
                    return UploadBindingRedemption(status="invalid")
                if datetime.fromisoformat(row["expires_at"]) <= redeemed_at:
                    return UploadBindingRedemption(status="expired")
                if row["consumed_at"]:
                    return UploadBindingRedemption(status="used")
                if row["c2c_user_openid"] != c2c_user_openid:
                    return UploadBindingRedemption(status="used")
                return UploadBindingRedemption(
                    status="already_redeemed",
                    binding=self._upload_binding_from_row(row),
                )

            connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND id != ?
                """,
                (redeemed_at.isoformat(), c2c_user_openid, row["id"]),
            )

        return UploadBindingRedemption(
            status="redeemed",
            binding=self._upload_binding_from_row(row),
        )

    def get_pending_upload_binding(
            self,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBinding | None:
        current_time = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM upload_bindings
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (c2c_user_openid, current_time.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return self._upload_binding_from_row(row)

    def claim_pending_upload_binding(
            self,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBinding | None:
        claimed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM upload_bindings
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (c2c_user_openid, claimed_at.isoformat()),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (claimed_at.isoformat(), row["id"]),
            )
            if cursor.rowcount != 1:
                return None
        return self._upload_binding_from_row(row)

    def consume_upload_binding(
            self,
            binding_id: int,
            now: datetime | None = None) -> None:
        consumed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (consumed_at.isoformat(), binding_id),
            )

    @staticmethod
    def _upload_binding_from_row(row: sqlite3.Row) -> UploadBinding:
        return UploadBinding(
            binding_id=int(row["id"]),
            group_openid=row["group_openid"],
            issuer_openid=row["issuer_openid"],
            c2c_user_openid=row["c2c_user_openid"] or "",
            expires_at=row["expires_at"],
        )

    def has_message(self, user_msg_id: str) -> bool:
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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

    def add_document(
            self,
            group_openid: str,
            uploader_openid: str,
            filename: str,
            sha256: str,
            full_text: str,
            chunks: list[str],
            summary: str = "") -> StoredDocument:
        created_at = datetime.now(timezone.utc).isoformat()
        preview = full_text[:500]

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, filename FROM documents
                WHERE group_openid = ? AND sha256 = ?
                """,
                (group_openid, sha256),
            ).fetchone()
            if existing:
                return StoredDocument(
                    document_id=existing["id"],
                    filename=existing["filename"],
                    is_new=False,
                )

            cursor = connection.execute(
                """
                INSERT INTO documents (
                    group_openid,
                    uploader_openid,
                    filename,
                    sha256,
                    preview,
                    summary,
                    text_length,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_openid,
                    uploader_openid,
                    filename,
                    sha256,
                    preview,
                    summary,
                    len(full_text),
                    created_at,
                ),
            )
            document_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO document_chunks (
                    document_id, chunk_index, content
                ) VALUES (?, ?, ?)
                """,
                [
                    (document_id, index, chunk)
                    for index, chunk in enumerate(chunks)
                ],
            )

        return StoredDocument(
            document_id=document_id,
            filename=filename,
            is_new=True,
        )

    def commit_private_document_event(
            self,
            event_id: str,
            claim_token: str,
            platform: str,
            binding_id: int,
            group_openid: str,
            uploader_openid: str,
            document: PreparedDocument,
            reply: str,
            target_user_openid: str,
            reply_to_id: str,
            document_group_openid: str | None = None,
            now: datetime | None = None) -> int:
        committed_at = now or datetime.now(timezone.utc)
        storage_group_openid = document_group_openid or group_openid
        lease_expires_at = committed_at + EVENT_PROCESSING_LEASE
        payload_json = json.dumps(
            {"content": reply, "msg_type": 0},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

            existing_outbox = connection.execute(
                "SELECT id FROM outbox_messages WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing_outbox is not None:
                return int(existing_outbox["id"])

            existing_document = connection.execute(
                """
                SELECT id
                FROM documents
                WHERE group_openid = ? AND sha256 = ?
                """,
                (storage_group_openid, document.sha256),
            ).fetchone()
            if existing_document is None:
                cursor = connection.execute(
                    """
                    INSERT INTO documents (
                        group_openid,
                        uploader_openid,
                        filename,
                        sha256,
                        preview,
                        summary,
                        text_length,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_group_openid,
                        uploader_openid,
                        document.filename,
                        document.sha256,
                        document.full_text[:500],
                        document.summary,
                        len(document.full_text),
                        committed_at.isoformat(),
                    ),
                )
                document_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO document_chunks (
                        document_id, chunk_index, content
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (document_id, index, chunk)
                        for index, chunk in enumerate(document.chunks)
                    ],
                )

            consumed = connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ?
                  AND group_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (
                    committed_at.isoformat(),
                    binding_id,
                    group_openid,
                    committed_at.isoformat(),
                ),
            )

            if consumed.rowcount != 1:
                raise RuntimeError("Upload binding is no longer available")

            updated = connection.execute(
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
                    f"上传旅行文档：{document.filename}",
                    committed_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
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
                ) VALUES (?, ?, 'private', ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event_id,
                    platform,
                    target_user_openid,
                    target_user_openid,
                    reply_to_id,
                    payload_json,
                    committed_at.isoformat(),
                    committed_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def commit_private_reply_event(
            self,
            event_id: str,
            claim_token: str,
            platform: str,
            binding_id: int,
            reply: str,
            target_user_openid: str,
            reply_to_id: str,
            now: datetime | None = None) -> int:
        committed_at = now or datetime.now(timezone.utc)
        lease_expires_at = committed_at + EVENT_PROCESSING_LEASE
        payload_json = json.dumps(
            {"content": reply, "msg_type": 0},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

            existing_outbox = connection.execute(
                "SELECT id FROM outbox_messages WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing_outbox is not None:
                return int(existing_outbox["id"])

            consumed = connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                """,
                (committed_at.isoformat(), binding_id),
            )
            if consumed.rowcount != 1:
                raise RuntimeError("Upload binding is no longer available")

            updated = connection.execute(
                """
                UPDATE processed_events
                SET prepared_reply = ?,
                    prepared_memory_content = NULL,
                    updated_at = ?,
                    lease_expires_at = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                """,
                (
                    reply,
                    committed_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
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
                ) VALUES (?, ?, 'private', ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event_id,
                    platform,
                    target_user_openid,
                    target_user_openid,
                    reply_to_id,
                    payload_json,
                    committed_at.isoformat(),
                    committed_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid)

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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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

    def build_document_context(
            self,
            group_openid: str,
            query: str,
            max_chars: int = MAX_DOCUMENT_CONTEXT_CHARS) -> str:
        if max_chars <= 0:
            return ""

        with self._connect() as connection:
            documents = connection.execute(
                """
                SELECT id, filename, preview, summary, created_at
                FROM documents
                WHERE group_openid = ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (group_openid,),
            ).fetchall()
            selected = self._select_document_chunks(
                connection,
                group_openid,
                query,
            )

        if not documents:
            return ""

        parts: list[str] = []
        query_terms = self._search_terms(query)
        if selected:
            relevant_limit = (
                max_chars
                if max_chars < 600
                else max(1, int(max_chars * 0.76))
            )
            self._append_context_part(
                parts,
                "与当前问题相关的文档片段：",
                relevant_limit,
                allow_truncate=True,
            )
            for index, row in enumerate(selected):
                remaining_entries = len(selected) - index
                available = (
                    relevant_limit - self._context_length(parts)
                    - (1 if parts else 0)
                )
                if available <= 0:
                    break
                header = (
                    f"[{row['filename']} / 片段 {row['chunk_index'] + 1}]"
                )
                per_entry = max(1, available // remaining_entries)
                excerpt_limit = max(1, per_entry - len(header) - 1)
                excerpt = self._match_centered_excerpt(
                    row["content"],
                    query_terms,
                    excerpt_limit,
                )
                self._append_context_part(
                    parts,
                    f"{header}\n{excerpt}",
                    relevant_limit,
                    allow_truncate=True,
                )

        overview_rows = []
        seen_document_ids = set()
        for row in [*selected, *documents]:
            document_id = int(row["document_id"] if "document_id" in row.keys() else row["id"])
            if document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            overview_rows.append(row)

        if self._remaining_context_chars(parts, max_chars) >= 30:
            self._append_context_part(
                parts,
                "群内已保存的旅行文档：",
                max_chars,
            )
            for document in overview_rows:
                remaining = self._remaining_context_chars(parts, max_chars)
                if remaining < 20:
                    break
                overview = document["summary"] or document["preview"]
                overview = re.sub(r"\s+", " ", overview).strip()
                filename = document["filename"]
                overview_limit = max(1, min(260, remaining - len(filename) - 4))
                overview = self._match_centered_excerpt(
                    overview,
                    query_terms,
                    overview_limit,
                )
                if not self._append_context_part(
                        parts,
                        f"- {filename}：{overview}",
                        max_chars,
                        allow_truncate=True):
                    break

        return "\n".join(parts)

    def _select_document_chunks(
            self,
            connection: sqlite3.Connection,
            group_openid: str,
            query: str) -> list[sqlite3.Row]:
        candidates: list[sqlite3.Row] = []
        seen_chunk_ids = set()

        fts_terms = self._fts_terms(query)
        if self._document_fts_available and fts_terms:
            fts_query = " OR ".join(f'"{term}"' for term in fts_terms)
            try:
                rows = connection.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        c.document_id,
                        c.chunk_index,
                        c.content,
                        d.filename,
                        d.preview,
                        d.summary,
                        d.id AS document_order
                    FROM document_chunks_fts
                    JOIN document_chunks c
                      ON c.id = document_chunks_fts.rowid
                    JOIN documents d ON d.id = c.document_id
                    WHERE document_chunks_fts MATCH ?
                      AND d.group_openid = ?
                    ORDER BY bm25(document_chunks_fts) ASC,
                             d.id DESC,
                             c.chunk_index ASC
                    LIMIT ?
                    """,
                    (fts_query, group_openid, MAX_DOCUMENT_CANDIDATES),
                ).fetchall()
                for row in rows:
                    candidates.append(row)
                    seen_chunk_ids.add(row["chunk_id"])
            except sqlite3.OperationalError:
                self._document_fts_available = False

        fallback_terms = sorted(
            self._search_terms(query),
            key=lambda term: (-len(term), term),
        )[:MAX_FALLBACK_QUERY_TERMS]
        should_use_fallback = (
            not self._document_fts_available
            or not fts_terms
            or not candidates
        )
        if fallback_terms and should_use_fallback:
            score_clauses = []
            score_parameters: list[object] = []
            match_clauses = []
            match_parameters: list[object] = []
            for term in fallback_terms:
                score_clauses.append(
                    "CASE WHEN instr(lower(c.content), ?) > 0 "
                    "THEN ? ELSE 0 END"
                )
                score_parameters.extend((term, max(1, len(term))))
                match_clauses.append("instr(lower(c.content), ?) > 0")
                match_parameters.append(term)

            rows = connection.execute(
                f"""
                SELECT
                    c.id AS chunk_id,
                    c.document_id,
                    c.chunk_index,
                    c.content,
                    d.filename,
                    d.preview,
                    d.summary,
                    d.id AS document_order,
                    ({' + '.join(score_clauses)}) AS match_score
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.group_openid = ?
                  AND ({' OR '.join(match_clauses)})
                ORDER BY match_score DESC,
                         d.id DESC,
                         c.chunk_index ASC
                LIMIT ?
                """,
                (
                    *score_parameters,
                    group_openid,
                    *match_parameters,
                    MAX_DOCUMENT_CANDIDATES,
                ),
            ).fetchall()
            for row in rows:
                if row["chunk_id"] in seen_chunk_ids:
                    continue
                candidates.append(row)
                seen_chunk_ids.add(row["chunk_id"])

        terms = self._search_terms(query)
        ranked = []
        for row in candidates:
            content = row["content"].lower()
            score = sum(
                content.count(term) * max(1, len(term))
                for term in terms
            )
            ranked.append(
                (score, row["document_order"], -row["chunk_index"], row)
            )
        ranked.sort(reverse=True, key=lambda item: item[:3])
        selected = [
            item[3] for item in ranked if item[0] > 0
        ][:MAX_DOCUMENT_CHUNKS]
        if selected:
            return selected

        newest = connection.execute(
            """
            SELECT
                c.id AS chunk_id,
                c.document_id,
                c.chunk_index,
                c.content,
                d.filename,
                d.preview,
                d.summary,
                d.id AS document_order
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.group_openid = ?
            ORDER BY d.id DESC, c.chunk_index ASC
            LIMIT 1
            """,
            (group_openid,),
        ).fetchone()
        return [newest] if newest else []

    @staticmethod
    def _fts_terms(text: str) -> tuple[str, ...]:
        normalized = text.lower()
        terms = set(re.findall(r"[a-z0-9]{3,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if len(sequence) >= 3:
                terms.update(
                    sequence[index:index + 3]
                    for index in range(len(sequence) - 2)
                )
        return tuple(sorted(terms))[:MAX_FTS_QUERY_TERMS]

    @staticmethod
    def _match_centered_excerpt(
            text: str,
            terms: set[str],
            max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text

        normalized = text.lower()
        match_position = None
        for term in sorted(terms, key=lambda value: (-len(value), value)):
            position = normalized.find(term)
            if position >= 0:
                match_position = position
                break

        body_limit = max(1, max_chars - 2)
        if match_position is None:
            start = 0
        else:
            start = max(0, match_position - body_limit // 3)
        end = min(len(text), start + body_limit)
        if end - start < body_limit:
            start = max(0, end - body_limit)

        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        available = max(0, max_chars - len(prefix) - len(suffix))
        return f"{prefix}{text[start:start + available]}{suffix}"

    @staticmethod
    def _context_length(parts: list[str]) -> int:
        return sum(len(part) for part in parts) + max(0, len(parts) - 1)

    @classmethod
    def _remaining_context_chars(
            cls,
            parts: list[str],
            max_chars: int) -> int:
        return max(0, max_chars - cls._context_length(parts))

    @classmethod
    def _append_context_part(
            cls,
            parts: list[str],
            text: str,
            max_chars: int,
            allow_truncate: bool = False) -> bool:
        separator_chars = 1 if parts else 0
        remaining = max_chars - cls._context_length(parts) - separator_chars
        if remaining <= 0:
            return False
        if len(text) > remaining:
            if not allow_truncate:
                return False
            if remaining == 1:
                text = "…"
            else:
                text = text[:remaining - 1].rstrip() + "…"
        parts.append(text)
        return True

    @staticmethod
    def _search_terms(text: str) -> set[str]:
        normalized = text.lower()
        terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if 2 <= len(sequence) <= 8:
                terms.add(sequence)
            terms.update(
                sequence[index:index + 2]
                for index in range(len(sequence) - 1)
            )

        stop_terms = {
            "什么", "怎么", "如何", "一下", "这个", "那个",
            "帮我", "看看", "可以", "我们", "你们",
        }
        return terms - stop_terms
