"""UploadRepository: scoped storage, no business routing."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from infrastructure.delivery_repository import EVENT_PROCESSING_LEASE
from infrastructure.document_repository import PreparedDocument



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


class UploadRepository:
    def __init__(self, store):
        self.store = store

    def create_upload_binding(
            self,
            code_hash: str,
            group_openid: str,
            issuer_openid: str,
            expires_at: datetime,
            now: datetime | None = None) -> int:
        created_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
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

    def create_upload_binding_for_event(
            self,
            code_hash: str,
            group_openid: str,
            issuer_openid: str,
            expires_at: datetime,
            event_id: str,
            claim_token: str,
            reply: str,
            now: datetime | None = None) -> str:
        created_at = now or datetime.now(timezone.utc)
        lease_expires_at = created_at + EVENT_PROCESSING_LEASE
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                """
                SELECT status, claim_token, prepared_reply
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
                "SELECT id FROM upload_bindings WHERE source_event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                prepared_reply = str(event["prepared_reply"] or "")
                if not prepared_reply:
                    raise RuntimeError("Upload binding reply is unavailable")
                return prepared_reply

            connection.execute(
                """
                INSERT INTO upload_bindings (
                    code_hash, group_openid, issuer_openid, created_at,
                    expires_at, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    code_hash,
                    group_openid,
                    issuer_openid,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                    event_id,
                ),
            )
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
                    "申请私聊上传文档绑定码",
                    created_at.isoformat(),
                    lease_expires_at.isoformat(),
                    event_id,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"Lost event processing lease: {event_id}")
        return reply

    def redeem_upload_binding(
            self,
            code_hash: str,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBindingRedemption:
        redeemed_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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

