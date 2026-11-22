"""Reservation persistence and atomic reminder delivery transitions."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo
from core.chat_transport import storage_scope_id
from infrastructure.delivery_repository import MAX_EVENT_ERROR_CHARS

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
    date_source: dict = field(default_factory=dict)


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
    plan_version: int = 0
    source_conflicts: tuple[str, ...] = ()


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


class ReservationRepository:
    def __init__(self, store):
        self.store = store

    def start_reservation_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            now: datetime | None = None,
            duration: timedelta = timedelta(minutes=30)) -> datetime:
        started_at = now or datetime.now(timezone.utc)
        expires_at = started_at + duration
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT INTO reservation_workflows (
                    platform, group_id, creator_id, started_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform, group_id, creator_id) DO UPDATE SET
                    started_at = excluded.started_at,
                    expires_at = excluded.expires_at
                """,
                (
                    platform,
                    group_id,
                    creator_id,
                    started_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return expires_at

    def reservation_workflow_is_active(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM reservation_workflows
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND julianday(expires_at) > julianday(?)
                """,
                (platform, group_id, creator_id, current.isoformat()),
            ).fetchone()
            if row is not None:
                return True
            connection.execute(
                """
                DELETE FROM reservation_workflows
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                """,
                (platform, group_id, creator_id),
            )
        return False

    def clear_reservation_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> bool:
        with self.store._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM reservation_workflows
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                """,
                (platform, group_id, creator_id),
            )
        return cursor.rowcount == 1

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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
            now: datetime | None = None,
            source_event_id: str = "") -> ReservationPlanRecord:
        created_at = now or datetime.now(timezone.utc)
        local_day = created_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y%m%d")
        prefix = f"R-{local_day}-"
        existing_code = ""
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if source_event_id:
                existing = connection.execute(
                    "SELECT plan_code FROM reservation_plans "
                    "WHERE source_event_id = ?",
                    (source_event_id,),
                ).fetchone()
                if existing is not None:
                    existing_code = str(existing["plan_code"])
            if existing_code:
                plan_code = existing_code
            else:
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
                        source_event_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        plan_code,
                        image_id,
                        platform,
                        group_id,
                        creator_id,
                        source_event_id or None,
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
                        SET public_code = ?, date_source_json = ? WHERE id = ?
                        """,
                        (f"A-{item_id:06d}", json.dumps(item.get("date_source", {}), ensure_ascii=False), item_id),
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
        with self.store._connect() as connection:
            connection.execute("BEGIN")
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
            plan_version=int(plan["plan_version"]),
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
        with self.store._connect() as connection:
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

    def update_reservation_draft_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            requires_reservation: bool,
            visit_date: date | None,
            booking_date: date | None,
            date_candidates: Sequence[date],
            custom_reminder_times: Sequence[datetime],
            reminder_policy: str,
            status: str,
            now: datetime | None = None) -> bool:
        return self.update_reservation_draft_items(
            platform=platform,
            group_id=group_id,
            creator_id=creator_id,
            plan_code=plan_code,
            updates=({
                "item_index": item_index,
                "requires_reservation": requires_reservation,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": date_candidates,
                "custom_reminder_times": custom_reminder_times,
                "reminder_policy": reminder_policy,
                "status": status,
            },),
            now=now,
        )

    def update_reservation_draft_items(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            updates: Sequence[Mapping[str, object]],
            now: datetime | None = None) -> bool:
        if not updates:
            return False
        updated_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
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

            plan_id = int(plan["id"])
            item_indices = [int(update["item_index"]) for update in updates]
            if len(set(item_indices)) != len(item_indices):
                raise ValueError("同一景点不能在一次批量修改中重复出现")
            placeholders = ",".join("?" for unused in item_indices)
            existing = connection.execute(
                f"""
                SELECT item_index FROM reservation_items
                WHERE plan_id = ? AND item_index IN ({placeholders})
                """,
                (plan_id, *item_indices),
            ).fetchall()
            if {int(row["item_index"]) for row in existing} != set(item_indices):
                raise ValueError("未找到预约项目")

            for update in updates:
                visit_date = update["visit_date"]
                booking_date = update["booking_date"]
                date_candidates = update["date_candidates"]
                custom_times = update["custom_reminder_times"]
                connection.execute(
                    """
                    UPDATE reservation_items
                    SET requires_reservation = ?,
                        visit_date = ?,
                        booking_date = ?,
                        date_candidates_json = ?,
                        custom_reminder_times_json = ?,
                        reminder_policy = ?,
                        status = ?,
                        updated_at = ?
                    WHERE plan_id = ? AND item_index = ?
                    """,
                    (
                        int(bool(update["requires_reservation"])),
                        visit_date.isoformat() if visit_date else None,
                        booking_date.isoformat() if booking_date else None,
                        json.dumps([
                            value.isoformat()
                            for value in date_candidates
                        ]),
                        json.dumps([
                            value.astimezone(
                                ZoneInfo("Asia/Shanghai")
                            ).isoformat()
                            for value in custom_times
                        ]),
                        str(update["reminder_policy"]),
                        str(update["status"]),
                        updated_at.isoformat(),
                        plan_id,
                        int(update["item_index"]),
                    ),
                )
        return True

    def claim_reservation_refresh(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            now: datetime | None = None) -> int | None:
        updated_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT id, refresh_revision
                FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND plan_code = ?
                  AND status = 'draft'
                """,
                (platform, group_id, creator_id, plan_code),
            ).fetchone()
            if plan is None:
                return None
            revision = int(plan["refresh_revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE reservation_plans
                SET refresh_revision = ?, updated_at = ?
                WHERE id = ?
                  AND status = 'draft'
                  AND refresh_revision = ?
                """,
                (
                    revision,
                    updated_at.isoformat(),
                    int(plan["id"]),
                    int(plan["refresh_revision"]),
                ),
            )
        return revision if cursor.rowcount == 1 else None

    def refresh_reservation_draft_items(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            updates: tuple[dict[str, object], ...],
            now: datetime | None = None) -> int:
        updated_at = now or datetime.now(timezone.utc)
        changed = 0
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT id, refresh_revision FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND plan_code = ?
                  AND status = 'draft'
                """,
                (platform, group_id, creator_id, plan_code),
            ).fetchone()
            if plan is None:
                return 0
            for update in updates:
                visit_date = update["visit_date"]
                booking_date = update["booking_date"]
                refresh_revision = int(update["refresh_revision"])
                if refresh_revision > int(plan["refresh_revision"]):
                    raise ValueError("refresh revision was not claimed")
                visit_date_text = (
                    visit_date.isoformat() if visit_date else None
                )
                booking_date_text = (
                    booking_date.isoformat() if booking_date else None
                )
                date_candidates_json = json.dumps([
                    value.isoformat()
                    for value in update["date_candidates"]
                ])
                current = connection.execute(
                    """
                    SELECT id, visit_date, booking_date,
                           date_candidates_json, status
                    FROM reservation_items
                    WHERE plan_id = ?
                      AND item_index = ?
                      AND requires_reservation = 1
                      AND visit_date IS NULL
                      AND status IN ('needs_input', 'not_scheduled')
                      AND refresh_revision < ?
                    """,
                    (
                        int(plan["id"]),
                        int(update["item_index"]),
                        refresh_revision,
                    ),
                ).fetchone()
                if current is None:
                    continue
                row_changed = (
                    current["visit_date"] != visit_date_text
                    or current["booking_date"] != booking_date_text
                    or current["date_candidates_json"]
                    != date_candidates_json
                    or current["status"] != update["status"]
                )
                cursor = connection.execute(
                    """
                    UPDATE reservation_items
                    SET visit_date = ?,
                        booking_date = ?,
                        date_candidates_json = ?,
                        status = ?,
                        refresh_revision = ?,
                        date_source_json = ?,
                        updated_at = ?
                    WHERE plan_id = ?
                      AND id = ?
                      AND requires_reservation = 1
                      AND visit_date IS NULL
                      AND status IN ('needs_input', 'not_scheduled')
                      AND refresh_revision < ?
                    """,
                    (
                        visit_date_text,
                        booking_date_text,
                        date_candidates_json,
                        update["status"],
                        refresh_revision,
                        json.dumps(update.get("date_source", {}), ensure_ascii=False),
                        updated_at.isoformat(),
                        int(plan["id"]),
                        int(current["id"]),
                        refresh_revision,
                    ),
                )
                if row_changed:
                    changed += cursor.rowcount
        return changed

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
            now: datetime | None = None,
            source_operation_key: str = "") -> bool:
        created_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
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
            if source_operation_key:
                existing = connection.execute(
                    "SELECT 1 FROM reservation_items "
                    "WHERE source_operation_key = ?",
                    (source_operation_key,),
                ).fetchone()
                if existing is not None:
                    return True
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
                    source_operation_key, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, '', '', '', ?, 1.0, ?, ?, ?, ?, ?, ?, '[]',
                    ?, ?, ?, ?, ?
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
                    source_operation_key or None,
                    created_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            item_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE reservation_items SET public_code = ?, "
                "date_source_json = ? WHERE id = ?",
                (f"A-{item_id:06d}", '{"origin":"manual"}', item_id),
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
        with self.store._connect() as connection:
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
            now: datetime | None = None,
            expected_version: int | None = None) -> ReservationPlanRecord:
        confirmed_at = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
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
                if expected_version is not None and int(plan["plan_version"]) != expected_version:
                    raise ValueError("预约计划版本已变化，请查看最新草稿后重新确认")
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
                  AND i.status IN ('confirmed', 'cancelled')
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
            UPDATE processed_events
            SET status = 'completed',
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error = NULL,
                prepared_reply = NULL,
                prepared_memory_content = NULL
            WHERE event_id IN (
                SELECT outbox_event_id
                FROM reservation_reminders
                WHERE reservation_item_id = ?
            )
              AND event_id IN (
                SELECT event_id
                FROM outbox_messages
                WHERE status IN ('pending', 'failed')
            )
            """,
            (item_id,),
        )
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
        with self.store._connect() as connection:
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
        with self.store._connect() as connection:
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
                  AND i.status IN ('confirmed', 'cancelled')
                """,
                (platform, group_id, creator_id, public_code),
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["id"])
            if row["status"] == "cancelled":
                return ReservationMutationResult(
                    self._reservation_item(row),
                    False,
                )
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
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT id, status FROM reservation_plans
                WHERE platform = ?
                  AND group_id = ?
                  AND creator_id = ?
                  AND plan_code = ?
                  AND status IN ('draft', 'confirmed', 'cancelled')
                """,
                (platform, group_id, creator_id, plan_code),
            ).fetchone()
            if plan is None:
                return None
            if plan["status"] == "cancelled":
                return False
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
            date_source=json.loads(row["date_source_json"]),
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

