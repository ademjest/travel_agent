"""Idempotent schema and compatibility migrations, using the caller database."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

DOCUMENT_FTS_MIGRATION = "document_chunks_fts_v1"


def initialize(store) -> None:
    with store._connect() as connection:
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
                    consumed_at TEXT,
                    source_event_id TEXT UNIQUE
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

                CREATE TABLE IF NOT EXISTS event_tool_results (
                    operation_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES processed_events(event_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_event_tool_results_event
                ON event_tool_results(event_id);

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

                CREATE TABLE IF NOT EXISTS reservation_workflows (
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(platform, group_id, creator_id)
                );

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
                    source_event_id TEXT,
                    status TEXT NOT NULL,
                    refresh_revision INTEGER NOT NULL DEFAULT 0,
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
                    refresh_revision INTEGER NOT NULL DEFAULT 0,
                    source_operation_key TEXT,
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

        reservation_plan_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reservation_plans)"
            ).fetchall()
        }
        if "plan_version" not in reservation_plan_columns:
            connection.execute(
                "ALTER TABLE reservation_plans ADD COLUMN "
                "plan_version INTEGER NOT NULL DEFAULT 0"
            )
        # Every item mutation advances the version in its own transaction.
        for operation, row_name in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            connection.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS reservation_version_{operation.lower()}
                    AFTER {operation} ON reservation_items
                    BEGIN
                        UPDATE reservation_plans SET plan_version = plan_version + 1
                        WHERE id = {row_name}.plan_id;
                    END
                """)
        connection.execute("""
                CREATE TRIGGER IF NOT EXISTS reservation_version_status
                AFTER UPDATE OF status ON reservation_plans
                WHEN NEW.status != OLD.status
                BEGIN
                    UPDATE reservation_plans SET plan_version = plan_version + 1
                    WHERE id = NEW.id;
                END
            """)
        if "refresh_revision" not in reservation_plan_columns:
            connection.execute(
                "ALTER TABLE reservation_plans ADD COLUMN "
                "refresh_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "source_event_id" not in reservation_plan_columns:
            connection.execute(
                "ALTER TABLE reservation_plans ADD COLUMN source_event_id TEXT"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_reservation_plan_source_event "
            "ON reservation_plans(source_event_id) "
            "WHERE source_event_id IS NOT NULL AND source_event_id != ''"
        )

        reservation_item_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reservation_items)"
            ).fetchall()
        }
        if "date_source_json" not in reservation_item_columns:
            connection.execute(
                "ALTER TABLE reservation_items ADD COLUMN "
                "date_source_json TEXT NOT NULL DEFAULT '{}'"
            )
        connection.execute("""
                CREATE TRIGGER IF NOT EXISTS reservation_manual_date_source
                AFTER UPDATE OF visit_date ON reservation_items
                WHEN NEW.visit_date IS NOT NULL AND NEW.date_source_json = OLD.date_source_json
                BEGIN
                    UPDATE reservation_items
                    SET date_source_json = '{"origin":"manual"}' WHERE id = NEW.id;
                END
            """)
        if "refresh_revision" not in reservation_item_columns:
            connection.execute(
                "ALTER TABLE reservation_items ADD COLUMN "
                "refresh_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "source_operation_key" not in reservation_item_columns:
            connection.execute(
                "ALTER TABLE reservation_items ADD COLUMN "
                "source_operation_key TEXT"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_reservation_item_source_operation "
            "ON reservation_items(source_operation_key) "
            "WHERE source_operation_key IS NOT NULL "
            "AND source_operation_key != ''"
        )

        upload_binding_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(upload_bindings)"
            ).fetchall()
        }
        if "source_event_id" not in upload_binding_columns:
            connection.execute(
                "ALTER TABLE upload_bindings ADD COLUMN source_event_id TEXT"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_upload_binding_source_event "
            "ON upload_bindings(source_event_id) "
            "WHERE source_event_id IS NOT NULL AND source_event_id != ''"
        )

        connection.execute(
            """
                CREATE INDEX IF NOT EXISTS idx_processed_events_status_lease
                ON processed_events(status, lease_expires_at)
                """
        )
        initialize_document_search(store, connection)


def initialize_document_search(
        store,
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
        store._document_fts_available = True
    except sqlite3.OperationalError:
        # Some custom SQLite builds omit FTS5 or the trigram tokenizer.
        # The query path below falls back to bounded SQL substring search.
        store._document_fts_available = False


